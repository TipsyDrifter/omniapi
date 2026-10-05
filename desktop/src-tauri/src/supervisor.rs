//! What to do about the service, decided from what was just observed — no I/O here, so every
//! rule is unit-tested. `service.rs` observes (health check, has our process exited), asks
//! [`Supervisor::tick`], and carries out the [`Action`].
//!
//! The rules:
//! * At start: answering → adopt it (never start a second one); answering but still booting →
//!   wait for it like for our own; silent → start one.
//! * A start counts as failed when our process exits before the service answers, or when it
//!   has not answered within `startup_timeout_secs`.
//! * While up: our process exiting = failed at once; `miss_threshold` silent checks in a row =
//!   failed (the process is taken down first — a hung service would otherwise hold the port).
//! * After a failure: wait `backoff_secs[n-1]` (n = failures in a row), start again. After
//!   `max_failures` in a row: stop trying (`Failed`, red icon, reason shown) until the user
//!   asks for a restart. No endless restart loop burning CPU.
//! * Up for `stable_secs` = the failure count goes back to zero, so a crash days later gets a
//!   fresh set of retries while a service that dies every few seconds still runs out of them.
//! * Somebody else's service showing up on the port while we wait or have given up (e.g. the old
//!   `omni autostart` launcher) is adopted instead of fought over.

use std::time::{Duration, Instant};

use crate::config::Watch;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// A start is in progress: waiting for `/api/health` to say ok.
    Starting,
    /// Answering.
    Up,
    /// Not answering: counting missed checks, or waiting before the next start.
    Down,
    /// Gave up after too many failed starts; waits for a manual restart.
    Failed,
    /// Quit or restart in progress.
    Stopping,
    /// The config says the shell does not manage a service.
    Disabled,
}

/// What `/api/health` said.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Health {
    Ok { pid: u32 },
    /// Answers, but the runtime is not ready yet (`status != "ok"`).
    Booting { pid: u32 },
    None,
}

/// The process this shell started, if any.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Child {
    /// We have no process of our own (adopted service, or nothing started).
    NotOurs,
    Running,
    Exited(Option<i32>),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Action {
    Nothing,
    /// Start the service.
    Spawn,
    /// Take down what is left of a failed service (`why` goes to the log).
    Kill { why: String },
}

pub struct Supervisor {
    pub watch: Watch,
    pub phase: Phase,
    /// pid that answers `/api/health` (the real service, not the launcher).
    pub pid: Option<u32>,
    /// The service was already running when we found it (not started by this shell).
    pub adopted: bool,
    /// Failed starts in a row.
    pub failures: u32,
    /// Starts after a failure or on request, this session.
    pub restarts: u32,
    /// Why the last start failed (stays until the service is up again).
    pub problem: Option<String>,
    misses: u32,
    started_at: Option<Instant>,
    up_since: Option<Instant>,
    retry_at: Option<Instant>,
}

impl Supervisor {
    pub fn new(watch: Watch, enabled: bool) -> Supervisor {
        Supervisor {
            watch,
            phase: if enabled { Phase::Starting } else { Phase::Disabled },
            pid: None,
            adopted: false,
            failures: 0,
            restarts: 0,
            problem: None,
            misses: 0,
            started_at: None,
            up_since: None,
            retry_at: None,
        }
    }

    /// First look at the port, before anything was started.
    pub fn boot(&mut self, h: Health, now: Instant) -> Action {
        if self.phase == Phase::Disabled {
            return Action::Nothing;
        }
        match h {
            Health::Ok { pid } => {
                self.adopt(pid, now);
                Action::Nothing
            }
            Health::Booting { pid } => {
                self.phase = Phase::Starting;
                self.pid = Some(pid);
                self.adopted = true;
                self.started_at = Some(now);
                Action::Nothing
            }
            Health::None => Action::Spawn,
        }
    }

    /// Our process was started.
    pub fn spawned(&mut self, now: Instant) {
        self.phase = Phase::Starting;
        self.adopted = false;
        self.pid = None;
        self.misses = 0;
        self.started_at = Some(now);
        self.up_since = None;
        self.retry_at = None;
    }

    /// Starting the process failed outright (program missing, access denied…).
    pub fn spawn_failed(&mut self, err: &str, now: Instant) {
        self.started_at = Some(now);
        self.fail(format!("無法執行服務程式：{err}"), now);
    }

    /// The user asked for a restart (also out of `Failed`): a fresh set of retries.
    pub fn manual_restart(&mut self) {
        self.failures = 0;
        self.problem = None;
        self.retry_at = None;
        self.phase = Phase::Stopping;
    }

    pub fn stopping(&mut self) {
        self.phase = Phase::Stopping;
        self.retry_at = None;
    }

    /// Seconds until the next start attempt, when one is scheduled.
    pub fn retry_in(&self, now: Instant) -> Option<Duration> {
        self.retry_at.map(|t| t.saturating_duration_since(now))
    }

    pub fn misses(&self) -> u32 {
        self.misses
    }

    pub fn tick(&mut self, h: Health, child: Child, now: Instant) -> Action {
        match self.phase {
            Phase::Disabled | Phase::Stopping => Action::Nothing,
            Phase::Failed => {
                if let Health::Ok { pid } = h {
                    self.adopt(pid, now); // someone started it by hand: watch it again
                }
                Action::Nothing
            }
            Phase::Starting => match h {
                Health::Ok { pid } => {
                    self.phase = Phase::Up;
                    self.pid = Some(pid);
                    self.misses = 0;
                    self.problem = None;
                    self.up_since = Some(now);
                    if matches!(child, Child::Exited(_)) {
                        self.adopted = true; // ours lost the race for the port to another one
                    }
                    Action::Nothing
                }
                _ if self.timed_out(now) => {
                    if let Health::Booting { pid } = h {
                        self.pid = Some(pid);
                    }
                    let secs = self.watch.startup_timeout_secs;
                    self.fail(format!("服務在 {secs} 秒內都沒有就緒"), now)
                }
                Health::Booting { pid } => {
                    self.pid = Some(pid);
                    Action::Nothing
                }
                Health::None => match child {
                    Child::Exited(code) => self.fail(format!("服務程式還沒就緒就結束了{}", code_text(code)), now),
                    // an adopted, still-booting service that went silent: no process of ours to
                    // watch, so count misses as if it were up
                    Child::NotOurs if self.adopted => {
                        self.misses += 1;
                        if self.misses >= self.watch.miss_threshold {
                            let n = self.misses;
                            return self.fail(format!("連續 {n} 次沒有回應"), now);
                        }
                        Action::Nothing
                    }
                    _ => Action::Nothing,
                },
            },
            Phase::Up | Phase::Down if self.retry_at.is_none() => match h {
                Health::Ok { pid } | Health::Booting { pid } => {
                    if self.phase == Phase::Down {
                        self.phase = Phase::Up;
                        self.up_since = Some(now);
                    }
                    self.pid = Some(pid);
                    self.misses = 0;
                    if self.up_since.is_some_and(|t| now.saturating_duration_since(t) >= Duration::from_secs(self.watch.stable_secs)) {
                        self.failures = 0;
                    }
                    Action::Nothing
                }
                Health::None => {
                    if let Child::Exited(code) = child {
                        return self.fail(format!("服務程式結束了{}", code_text(code)), now);
                    }
                    self.misses += 1;
                    self.phase = Phase::Down;
                    if self.misses >= self.watch.miss_threshold {
                        let n = self.misses;
                        return self.fail(format!("連續 {n} 次沒有回應"), now);
                    }
                    Action::Nothing
                }
            },
            // Down, waiting to start again
            Phase::Up | Phase::Down => {
                if let Health::Ok { pid } = h {
                    self.adopt(pid, now);
                    return Action::Nothing;
                }
                if self.retry_at.is_some_and(|t| now >= t) {
                    self.retry_at = None;
                    self.restarts += 1;
                    return Action::Spawn;
                }
                Action::Nothing
            }
        }
    }

    fn adopt(&mut self, pid: u32, now: Instant) {
        self.phase = Phase::Up;
        self.pid = Some(pid);
        self.adopted = true;
        self.misses = 0;
        self.problem = None;
        self.retry_at = None;
        self.up_since = Some(now);
    }

    fn timed_out(&self, now: Instant) -> bool {
        self.started_at.is_some_and(|t| now.saturating_duration_since(t) >= Duration::from_secs(self.watch.startup_timeout_secs))
    }

    /// One more failure in a row: schedule the next start, or give up.
    fn fail(&mut self, why: String, now: Instant) -> Action {
        self.failures += 1;
        self.misses = 0;
        self.up_since = None;
        let kill = Action::Kill { why: why.clone() };
        if self.failures >= self.watch.max_failures {
            self.phase = Phase::Failed;
            self.retry_at = None;
            self.problem = Some(format!("{why}；連續 {} 次啟動失敗，已停止重試", self.failures));
        } else {
            let b = &self.watch.backoff_secs;
            let wait = b[(self.failures as usize - 1).min(b.len() - 1)];
            self.phase = Phase::Down;
            self.retry_at = Some(now + Duration::from_secs(wait));
            self.problem = Some(why);
        }
        kill
    }
}

fn code_text(code: Option<i32>) -> String {
    match code {
        Some(c) => format!("（結束代碼 {c}）"),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn watch() -> Watch {
        Watch { max_failures: 3, backoff_secs: vec![0, 5], miss_threshold: 3, startup_timeout_secs: 60, stable_secs: 30, ..Watch::default() }
    }

    fn s(n: u64) -> Duration {
        Duration::from_secs(n)
    }

    const OK: Health = Health::Ok { pid: 7 };

    #[test]
    fn adopts_a_running_service_instead_of_starting_one() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        assert_eq!(sv.boot(OK, t), Action::Nothing);
        assert_eq!((sv.phase, sv.pid, sv.adopted), (Phase::Up, Some(7), true));
    }

    #[test]
    fn waits_for_a_booting_service_it_found() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        assert_eq!(sv.boot(Health::Booting { pid: 9 }, t), Action::Nothing);
        assert_eq!((sv.phase, sv.adopted), (Phase::Starting, true));
        assert_eq!(sv.tick(OK, Child::NotOurs, t + s(3)), Action::Nothing);
        assert_eq!(sv.phase, Phase::Up);
    }

    #[test]
    fn silent_port_means_start_one_then_up() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        assert_eq!(sv.boot(Health::None, t), Action::Spawn);
        sv.spawned(t);
        assert_eq!(sv.tick(Health::None, Child::Running, t + s(10)), Action::Nothing, "still importing");
        assert_eq!(sv.phase, Phase::Starting);
        assert_eq!(sv.tick(OK, Child::Running, t + s(12)), Action::Nothing);
        assert_eq!((sv.phase, sv.adopted, sv.failures), (Phase::Up, false, 0));
    }

    #[test]
    fn disabled_does_nothing() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), false);
        assert_eq!(sv.boot(Health::None, t), Action::Nothing);
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(100)), Action::Nothing);
        assert_eq!(sv.phase, Phase::Disabled);
    }

    #[test]
    fn a_process_that_exits_at_once_runs_out_of_retries_with_backoff() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        assert_eq!(sv.boot(Health::None, t), Action::Spawn);
        sv.spawned(t);
        // failure 1: retry at once (backoff[0] = 0)
        assert!(matches!(sv.tick(Health::None, Child::Exited(Some(3)), t + s(1)), Action::Kill { .. }));
        assert_eq!((sv.phase, sv.failures), (Phase::Down, 1));
        assert!(sv.problem.as_deref().unwrap().contains("結束代碼 3"));
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(1)), Action::Spawn);
        sv.spawned(t + s(1));
        // failure 2: wait 5 s
        assert!(matches!(sv.tick(Health::None, Child::Exited(Some(3)), t + s(2)), Action::Kill { .. }));
        assert_eq!(sv.retry_in(t + s(2)), Some(s(5)));
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(4)), Action::Nothing, "still backing off");
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(7)), Action::Spawn);
        sv.spawned(t + s(7));
        // failure 3 = max_failures: give up, no more spawns however long we wait
        assert!(matches!(sv.tick(Health::None, Child::Exited(Some(3)), t + s(8)), Action::Kill { .. }));
        assert_eq!(sv.phase, Phase::Failed);
        assert!(sv.problem.as_deref().unwrap().contains("已停止重試"));
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(3600)), Action::Nothing);
        assert_eq!(sv.restarts, 2);
    }

    #[test]
    fn the_last_backoff_entry_repeats() {
        let t = Instant::now();
        let mut sv = Supervisor::new(Watch { max_failures: 10, backoff_secs: vec![1, 2], ..watch() }, true);
        sv.spawned(t);
        let mut now = t;
        for expect in [1, 2, 2, 2] {
            now += s(1);
            assert!(matches!(sv.tick(Health::None, Child::Exited(None), now), Action::Kill { .. }));
            assert_eq!(sv.retry_in(now), Some(s(expect)));
            now += s(expect);
            assert_eq!(sv.tick(Health::None, Child::NotOurs, now), Action::Spawn);
            sv.spawned(now);
        }
    }

    #[test]
    fn spawn_error_counts_as_a_failure() {
        let t = Instant::now();
        let mut sv = Supervisor::new(Watch { max_failures: 1, ..watch() }, true);
        sv.spawn_failed("找不到檔案", t);
        assert_eq!(sv.phase, Phase::Failed);
        assert!(sv.problem.as_deref().unwrap().starts_with("無法執行服務程式：找不到檔案"));
    }

    #[test]
    fn start_that_never_answers_times_out() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        sv.spawned(t);
        assert_eq!(sv.tick(Health::Booting { pid: 5 }, Child::Running, t + s(59)), Action::Nothing);
        assert!(matches!(sv.tick(Health::Booting { pid: 5 }, Child::Running, t + s(60)), Action::Kill { .. }));
        assert!(sv.problem.as_deref().unwrap().contains("60 秒"));
        assert_eq!(sv.pid, Some(5), "the hung pid is known so it can be killed");
    }

    #[test]
    fn misses_while_up_restart_after_the_threshold() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        sv.boot(OK, t);
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(3)), Action::Nothing);
        assert_eq!((sv.phase, sv.misses()), (Phase::Down, 1));
        assert_eq!(sv.tick(OK, Child::NotOurs, t + s(6)), Action::Nothing, "one blip, back");
        assert_eq!((sv.phase, sv.misses()), (Phase::Up, 0));
        sv.tick(Health::None, Child::NotOurs, t + s(9));
        sv.tick(Health::None, Child::NotOurs, t + s(12));
        let a = sv.tick(Health::None, Child::NotOurs, t + s(15));
        assert_eq!(a, Action::Kill { why: "連續 3 次沒有回應".into() });
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(15)), Action::Spawn, "first retry is immediate");
    }

    #[test]
    fn our_process_exiting_while_up_restarts_at_once() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        sv.spawned(t);
        sv.tick(OK, Child::Running, t + s(5));
        assert!(matches!(sv.tick(Health::None, Child::Exited(Some(1)), t + s(8)), Action::Kill { .. }));
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(8)), Action::Spawn);
    }

    #[test]
    fn staying_up_resets_the_failure_count() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        sv.spawned(t);
        sv.tick(Health::None, Child::Exited(None), t + s(1));
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(1)), Action::Spawn);
        sv.spawned(t + s(1));
        sv.tick(OK, Child::Running, t + s(5));
        assert_eq!(sv.failures, 1, "not stable yet");
        sv.tick(OK, Child::Running, t + s(40));
        assert_eq!(sv.failures, 0, "up for stable_secs");
        assert_eq!(sv.problem, None);
    }

    #[test]
    fn flapping_service_still_gives_up() {
        // answers briefly, dies, again and again: never stable, so failures add up
        let t = Instant::now();
        let mut sv = Supervisor::new(Watch { backoff_secs: vec![0], ..watch() }, true);
        sv.spawned(t);
        let mut now = t;
        for _ in 0..3 {
            now += s(5);
            sv.tick(OK, Child::Running, now);
            now += s(5);
            assert!(matches!(sv.tick(Health::None, Child::Exited(Some(1)), now), Action::Kill { .. }));
            if sv.phase == Phase::Failed {
                break;
            }
            assert_eq!(sv.tick(Health::None, Child::NotOurs, now), Action::Spawn);
            sv.spawned(now);
        }
        assert_eq!(sv.phase, Phase::Failed);
    }

    #[test]
    fn another_service_appearing_while_waiting_or_failed_is_adopted() {
        let t = Instant::now();
        let mut sv = Supervisor::new(Watch { backoff_secs: vec![30], max_failures: 2, ..watch() }, true);
        sv.spawned(t);
        sv.tick(Health::None, Child::Exited(None), t + s(1));
        assert_eq!(sv.tick(Health::Ok { pid: 99 }, Child::NotOurs, t + s(3)), Action::Nothing);
        assert_eq!((sv.phase, sv.pid, sv.adopted), (Phase::Up, Some(99), true));

        let mut sv = Supervisor::new(Watch { max_failures: 1, ..watch() }, true);
        sv.spawned(t);
        sv.tick(Health::None, Child::Exited(None), t + s(1));
        assert_eq!(sv.phase, Phase::Failed);
        sv.tick(Health::Ok { pid: 98 }, Child::NotOurs, t + s(9));
        assert_eq!((sv.phase, sv.pid), (Phase::Up, Some(98)));
    }

    #[test]
    fn losing_the_port_race_means_adopting_the_winner() {
        let t = Instant::now();
        let mut sv = Supervisor::new(watch(), true);
        sv.spawned(t);
        assert_eq!(sv.tick(Health::Ok { pid: 11 }, Child::Exited(Some(1)), t + s(4)), Action::Nothing);
        assert_eq!((sv.phase, sv.adopted), (Phase::Up, true));
    }

    #[test]
    fn manual_restart_out_of_failed_gets_fresh_retries() {
        let t = Instant::now();
        let mut sv = Supervisor::new(Watch { max_failures: 1, ..watch() }, true);
        sv.spawned(t);
        sv.tick(Health::None, Child::Exited(None), t + s(1));
        assert_eq!(sv.phase, Phase::Failed);
        sv.manual_restart();
        assert_eq!((sv.phase, sv.failures, sv.problem.clone()), (Phase::Stopping, 0, None));
        assert_eq!(sv.tick(Health::None, Child::NotOurs, t + s(2)), Action::Nothing, "the runner starts it, not the tick");
        sv.spawned(t + s(2));
        assert_eq!(sv.phase, Phase::Starting);
    }
}
