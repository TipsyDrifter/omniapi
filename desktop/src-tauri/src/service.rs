//! The service's life, carried out on one thread ("service-runner"): adopt or start at boot,
//! health checks, restarts with backoff (rules in `supervisor.rs`), and stopping on quit.
//!
//! Everything that touches the service goes through this thread, in order: the tray, the window
//! and second launches only send it [`Command`]s and read [`Snapshot`]s. A restart or quit that
//! takes 15 s therefore never races a health check that would start the service again.
//!
//! Stopping (quit, restart): ask `POST /api/shutdown` first and wait for the service to finish by
//! itself (replies and generations in flight are closed out as on any shutdown); only when it
//! has not gone after `shutdown_grace_secs` is the whole tree killed. A hung service found by the
//! health checks is killed straight away — asking it would only add a timeout.

use std::fs::OpenOptions;
use std::path::PathBuf;
use std::process::{Child as ProcChild, Command as ProcCommand, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::config::ShellConfig;
use crate::http;
use crate::log::{epoch_ms, Logger};
use crate::proc::{self, Job, ProcInfo};
use crate::supervisor::{Action, Child, Health, Phase, Supervisor};

pub enum Command {
    Restart(String),
    /// Stop the service, then answer on the channel.
    Quit(Sender<()>),
}

/// What the tray and the window show.
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub phase: Phase,
    pub port: u16,
    pub pid: Option<u32>,
    pub adopted: bool,
    pub failures: u32,
    pub max_failures: u32,
    pub misses: u32,
    pub miss_threshold: u32,
    pub restarts: u32,
    pub problem: Option<String>,
    pub retry_in_secs: Option<u64>,
    /// When the current start began (Unix ms), for the starting page's "waited N s".
    pub starting_since_ms: Option<u64>,
}

impl Snapshot {
    pub fn to_json(&self, log_dir: &str) -> serde_json::Value {
        serde_json::json!({
            "phase": phase_name(self.phase),
            "port": self.port,
            "pid": self.pid,
            "adopted": self.adopted,
            "failures": self.failures,
            "maxFailures": self.max_failures,
            "restarts": self.restarts,
            "problem": self.problem,
            "retryInSecs": self.retry_in_secs,
            "startingSinceMs": self.starting_since_ms,
            "logDir": log_dir,
        })
    }
}

pub fn phase_name(p: Phase) -> &'static str {
    match p {
        Phase::Starting => "starting",
        Phase::Up => "up",
        Phase::Down => "down",
        Phase::Failed => "failed",
        Phase::Stopping => "stopping",
        Phase::Disabled => "disabled",
    }
}

pub type OnChange = Arc<dyn Fn(&Snapshot) + Send + Sync>;

/// The handle the rest of the shell holds.
#[derive(Clone)]
pub struct Service {
    pub cfg: Arc<ShellConfig>,
    tx: Sender<Command>,
    snap: Arc<Mutex<Snapshot>>,
}

impl Service {
    /// Start the runner thread (it adopts or starts the service right away).
    pub fn start(cfg: ShellConfig, log: Arc<Logger>, on_change: OnChange) -> Service {
        let cfg = Arc::new(cfg);
        let (tx, rx) = mpsc::channel();
        let sup = Supervisor::new(cfg.watch.clone(), cfg.service.enabled);
        let snap = Arc::new(Mutex::new(Snapshot {
            phase: sup.phase,
            port: cfg.port,
            pid: None,
            adopted: false,
            failures: 0,
            max_failures: cfg.watch.max_failures,
            misses: 0,
            miss_threshold: cfg.watch.miss_threshold,
            restarts: 0,
            problem: None,
            retry_in_secs: None,
            starting_since_ms: None,
        }));
        let runner = Runner {
            cfg: Arc::clone(&cfg),
            log,
            sup,
            child: None,
            job: None,
            starting_since_ms: None,
            rx,
            snap: Arc::clone(&snap),
            on_change,
        };
        std::thread::Builder::new().name("service-runner".into()).spawn(move || runner.run()).expect("spawn service-runner");
        Service { cfg, tx, snap }
    }

    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}/", self.cfg.port)
    }

    pub fn snapshot(&self) -> Snapshot {
        self.snap.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn restart(&self, via: &str) {
        let _ = self.tx.send(Command::Restart(via.to_string()));
    }

    /// Stop the service and wait for it (bounded: grace + kill + margin).
    pub fn quit(&self) -> bool {
        let (done_tx, done_rx) = mpsc::channel();
        if self.tx.send(Command::Quit(done_tx)).is_err() {
            return false;
        }
        let limit = Duration::from_secs(self.cfg.watch.shutdown_grace_secs + 30);
        done_rx.recv_timeout(limit).is_ok()
    }
}

struct Runner {
    cfg: Arc<ShellConfig>,
    log: Arc<Logger>,
    sup: Supervisor,
    /// The launcher we started (the venv's pythonw.exe), while it is ours.
    child: Option<ProcChild>,
    job: Option<Job>,
    starting_since_ms: Option<u64>,
    rx: Receiver<Command>,
    snap: Arc<Mutex<Snapshot>>,
    on_change: OnChange,
}

impl Runner {
    fn run(mut self) {
        if self.sup.phase == Phase::Disabled {
            self.log.line("service-disabled", "config service.enabled=false: not starting, watching or stopping anything");
        } else {
            let h = http::health(self.cfg.port);
            if let Health::Ok { pid } | Health::Booting { pid } = h {
                let ver = http::version(self.cfg.port).unwrap_or_else(|| "?".into());
                let img = proc::image_path(pid).unwrap_or_default();
                self.log.line("adopt", format!("service already answering on :{} pid={pid} v{ver} exe={img:?}; not starting another", self.cfg.port));
            }
            let a = self.sup.boot(h, Instant::now());
            if self.sup.phase == Phase::Starting && self.sup.adopted {
                self.starting_since_ms = Some(epoch_ms());
            }
            self.apply(a);
        }
        self.publish();
        loop {
            let wait = match self.sup.phase {
                Phase::Starting | Phase::Down => Duration::from_secs(1),
                _ => self.cfg.watch.poll(),
            };
            match self.rx.recv_timeout(wait) {
                Ok(Command::Restart(via)) => self.restart(&via),
                Ok(Command::Quit(done)) => {
                    if self.sup.phase != Phase::Disabled {
                        self.sup.stopping();
                        self.publish();
                        self.stop(true, "quit");
                    }
                    let _ = done.send(());
                    return;
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => return,
            }
            let before = self.sup.phase;
            let h = http::health(self.cfg.port);
            let c = self.child_state();
            let a = self.sup.tick(h, c, Instant::now());
            if self.sup.phase != before {
                self.log_transition(before, h);
            }
            if matches!(c, Child::Exited(_)) && self.sup.phase == Phase::Up {
                self.child = None; // lost the port race; the one answering is adopted
                self.job = None;
            }
            self.apply(a);
            self.publish();
        }
    }

    fn child_state(&mut self) -> Child {
        match self.child.as_mut().map(|c| c.try_wait()) {
            None => Child::NotOurs,
            Some(Ok(None)) => Child::Running,
            Some(Ok(Some(st))) => Child::Exited(st.code()),
            Some(Err(_)) => Child::Running,
        }
    }

    fn log_transition(&self, before: Phase, h: Health) {
        let st = &self.sup;
        let detail = match st.phase {
            Phase::Up => {
                let took = self.starting_since_ms.map(|t| format!(" after {:.1}s", (epoch_ms().saturating_sub(t)) as f64 / 1000.0)).unwrap_or_default();
                format!("pid={:?} adopted={}{took}", st.pid, st.adopted)
            }
            Phase::Down => format!("misses={}/{} problem={:?} retry_in={:?}", st.misses(), st.watch.miss_threshold, st.problem, st.retry_in(Instant::now())),
            Phase::Failed => format!("giving up: {:?}", st.problem),
            _ => format!("health={h:?}"),
        };
        self.log.line(&format!("phase-{}", phase_name(st.phase)), format!("from {} {detail}", phase_name(before)));
    }

    /// Carry out what the supervisor asked for. A kill is followed by another look, which may
    /// start the service again right away (first retry has no wait).
    fn apply(&mut self, mut a: Action) {
        for _ in 0..3 {
            match a {
                Action::Nothing => return,
                Action::Spawn => {
                    self.spawn();
                    return;
                }
                Action::Kill { why } => {
                    self.log.line("failed", format!("{why} (failures in a row {}/{})", self.sup.failures, self.sup.watch.max_failures));
                    self.kill_tree(&why);
                    if self.sup.phase == Phase::Failed {
                        self.log.line("give-up", format!("not starting the service again until asked: {:?}", self.sup.problem));
                    }
                    a = self.sup.tick(Health::None, Child::NotOurs, Instant::now());
                }
            }
        }
    }

    fn console_log(&self) -> PathBuf {
        self.cfg.log_dir.join("service.console.log")
    }

    fn spawn(&mut self) {
        let s = &self.cfg.service;
        let _ = std::fs::create_dir_all(&self.cfg.log_dir);
        let _ = std::fs::create_dir_all(&self.cfg.data_home);
        for d in &s.create_dirs {
            let _ = std::fs::create_dir_all(d);
        }
        let mut cmd = ProcCommand::new(&s.program);
        cmd.args(&s.args).current_dir(&s.cwd).stdin(Stdio::null());
        for (k, v) in &s.env {
            cmd.env(k, v);
        }
        // What the service prints before it has set up its own log (an import error, say).
        match OpenOptions::new().create(true).append(true).open(self.console_log()) {
            Ok(f) => {
                let f2 = f.try_clone().ok();
                cmd.stdout(Stdio::from(f));
                cmd.stderr(f2.map(Stdio::from).unwrap_or_else(Stdio::null));
            }
            Err(_) => {
                cmd.stdout(Stdio::null()).stderr(Stdio::null());
            }
        }
        let job = Job::new();
        let now = Instant::now();
        match proc::spawn_in_job(&mut cmd, job.as_ref()) {
            Ok((child, in_job)) => {
                self.log.line("spawn", format!("launcher_pid={} in_job={in_job} program={:?} args={:?} cwd={:?}", child.id(), s.program, s.args, s.cwd));
                self.child = Some(child);
                self.job = job;
                self.sup.spawned(now);
                self.starting_since_ms = Some(epoch_ms());
            }
            Err(e) => {
                self.log.line("spawn-failed", format!("program={:?} cwd={:?}: {e}", s.program, s.cwd));
                self.sup.spawn_failed(&e.to_string(), now);
                self.starting_since_ms = None;
                if self.sup.phase == Phase::Failed {
                    self.log.line("give-up", format!("not starting the service again until asked: {:?}", self.sup.problem));
                }
            }
        }
    }

    fn restart(&mut self, via: &str) {
        if self.sup.phase == Phase::Disabled {
            self.log.line("restart-ignored", format!("via={via}: service management is disabled in the config"));
            return;
        }
        self.log.line("restart", format!("requested via {via}"));
        self.sup.manual_restart();
        self.sup.restarts += 1;
        self.publish();
        self.stop(true, "restart");
        self.spawn();
    }

    /// The processes that make up the service: our launcher, the pid that answers health (only
    /// when it is a Python — a recycled pid must never get us to kill a stranger) and, for an
    /// adopted service, its parent when that is the configured program (the venv launcher).
    fn roots(&mut self, health_pid: Option<u32>) -> Vec<u32> {
        let mut roots = Vec::new();
        if let Some(c) = &self.child {
            roots.push(c.id());
        }
        if let Some(pid) = health_pid {
            let img = proc::image_path(pid).unwrap_or_default();
            let name = img.rsplit('\\').next().unwrap_or("").to_ascii_lowercase();
            if name.starts_with("python") {
                roots.push(pid);
                if let Some(pp) = proc::parent_of(pid) {
                    if proc::image_path(pp).is_some_and(|p| proc::same_path(&p, &self.cfg.service.program)) {
                        roots.push(pp);
                    }
                }
            }
        }
        roots
    }

    /// Kill without asking (a failed or hung service).
    fn kill_tree(&mut self, why: &str) {
        // the last pid health reported (a hung service does not answer any more; asking again
        // would only cost a connect timeout)
        let pid = self.sup.pid;
        let roots = self.roots(pid);
        let tree = proc::tree(&roots);
        let report = self.force(&tree);
        self.reap();
        self.wait_port_silent(Duration::from_secs(5));
        self.log.line("kill", format!("why={why} roots={roots:?} {report}"));
    }

    /// Quit / restart: ask first, kill only after the grace period.
    fn stop(&mut self, graceful: bool, why: &str) {
        let t0 = Instant::now();
        let h = http::health(self.cfg.port);
        let pid = match h {
            Health::Ok { pid } | Health::Booting { pid } => Some(pid),
            Health::None => self.sup.pid,
        };
        let roots = self.roots(pid);
        let tree = proc::tree(&roots);
        if tree.is_empty() && h == Health::None {
            self.reap();
            self.log.line("stop", format!("why={why} nothing was running"));
            return;
        }
        let mut how = "forced";
        if graceful && h != Health::None {
            match http::shutdown(self.cfg.port) {
                Ok(p) => {
                    self.log.line("shutdown-requested", format!("why={why} POST /api/shutdown accepted by pid={p}; waiting up to {}s", self.cfg.watch.shutdown_grace_secs));
                    let grace = Duration::from_secs(self.cfg.watch.shutdown_grace_secs);
                    let port = self.cfg.port;
                    let gone = wait_until(grace, || tree.iter().all(|p| !proc::is_alive(p.pid)) && http::health(port) == Health::None);
                    if gone {
                        how = "graceful";
                    } else {
                        self.log.line("shutdown-timeout", format!("still running after {}s; killing the tree", self.cfg.watch.shutdown_grace_secs));
                    }
                }
                Err(e) => self.log.line("shutdown-refused", format!("why={why} POST /api/shutdown: {e}; killing the tree")),
            }
        }
        let report = if how == "graceful" { "terminated=[] (exited by itself)".to_string() } else { self.force(&tree) };
        self.reap();
        self.wait_port_silent(Duration::from_secs(5));
        let left: Vec<String> = tree.iter().filter(|p| proc::is_alive(p.pid)).map(|p| format!("{}:{}", p.pid, p.exe)).collect();
        self.log.line(
            "stopped",
            format!(
                "why={why} how={how} took={:.1}s tree=[{}] {report} still_alive={left:?}",
                t0.elapsed().as_secs_f64(),
                tree.iter().map(|p| format!("{}:{}", p.pid, p.exe)).collect::<Vec<_>>().join(",")
            ),
        );
    }

    /// Terminate the job (everything ever started in it) and every process of `tree`.
    fn force(&mut self, tree: &[ProcInfo]) -> String {
        let job_killed = self.job.as_ref().map(|j| j.terminate()).unwrap_or(false);
        let killed: Vec<String> = tree.iter().filter(|p| proc::terminate(p.pid)).map(|p| format!("{}:{}", p.pid, p.exe)).collect();
        let _ = wait_until(Duration::from_secs(3), || tree.iter().all(|p| !proc::is_alive(p.pid)));
        format!("job_terminated={job_killed} terminated=[{}]", killed.join(","))
    }

    fn reap(&mut self) {
        if let Some(mut c) = self.child.take() {
            let _ = c.wait();
        }
        self.job = None;
        self.starting_since_ms = None;
    }

    /// So a new start does not race the old process for the port.
    fn wait_port_silent(&self, max: Duration) {
        let port = self.cfg.port;
        let _ = wait_until(max, || http::health(port) == Health::None);
    }

    fn publish(&mut self) {
        let now = Instant::now();
        let st = &self.sup;
        let snap = Snapshot {
            phase: st.phase,
            port: self.cfg.port,
            pid: st.pid,
            adopted: st.adopted,
            failures: st.failures,
            max_failures: st.watch.max_failures,
            misses: st.misses(),
            miss_threshold: st.watch.miss_threshold,
            restarts: st.restarts,
            problem: st.problem.clone(),
            retry_in_secs: st.retry_in(now).map(|d| d.as_secs_f64().ceil() as u64),
            starting_since_ms: if st.phase == Phase::Starting { self.starting_since_ms } else { None },
        };
        let changed = {
            let mut cur = self.snap.lock().unwrap_or_else(|e| e.into_inner());
            let changed = *cur != snap;
            *cur = snap.clone();
            changed
        };
        if changed {
            (self.on_change)(&snap);
        }
    }
}

fn wait_until(max: Duration, mut cond: impl FnMut() -> bool) -> bool {
    let t0 = Instant::now();
    loop {
        if cond() {
            return true;
        }
        if t0.elapsed() >= max {
            return false;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}
