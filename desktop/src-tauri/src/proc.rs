//! Windows process plumbing: start the service inside a Job Object, find a process tree,
//! take it down.
//!
//! Why a job: the venv's `pythonw.exe` is a launcher that immediately starts the real
//! `python.exe` (health answers with the child's pid; its parent is the launcher). Killing only
//! one of them can orphan the other; a job lets "stop" take everything the service ever started,
//! even grandchildren whose parent already died (`taskkill /T` cannot see those).
//!
//! The job is NOT kill-on-close: when the shell crashes or the installer kills it, the service
//! keeps running (1.3-b: MCP clients talk to the service directly) and the next shell adopts it
//! through `/api/health`. An adopted service has no job; its tree is found from the health pid.

use std::collections::{HashMap, HashSet, VecDeque};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command};

use windows_sys::Win32::Foundation::{CloseHandle, FILETIME, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, Thread32First, Thread32Next, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    TH32CS_SNAPTHREAD, THREADENTRY32,
};
use windows_sys::Win32::System::JobObjects::{AssignProcessToJobObject, CreateJobObjectW, TerminateJobObject};
use windows_sys::Win32::System::Threading::{
    GetExitCodeProcess, GetProcessTimes, OpenProcess, OpenThread, QueryFullProcessImageNameW, ResumeThread, TerminateProcess,
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_TERMINATE, THREAD_SUSPEND_RESUME,
};

pub struct Job(HANDLE);
// A job handle is a kernel handle; using it from another thread is fine.
unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Job {
    pub fn new() -> Option<Job> {
        let h = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        (!h.is_null()).then_some(Job(h))
    }
    pub fn terminate(&self) -> bool {
        unsafe { TerminateJobObject(self.0, 1) != 0 }
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

/// Start `cmd` suspended, put it in `job`, then let it run — so not even the launcher's first
/// child can be created outside the job. Returns whether the job assignment worked.
pub fn spawn_in_job(cmd: &mut Command, job: Option<&Job>) -> std::io::Result<(Child, bool)> {
    cmd.creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    let child = cmd.spawn()?;
    let assigned = match job {
        Some(j) => unsafe { AssignProcessToJobObject(j.0, child.as_raw_handle() as HANDLE) != 0 },
        None => false,
    };
    resume_all_threads(child.id());
    Ok((child, assigned))
}

fn resume_all_threads(pid: u32) {
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snap == INVALID_HANDLE_VALUE {
            return;
        }
        let mut te: THREADENTRY32 = std::mem::zeroed();
        te.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
        let mut ok = Thread32First(snap, &mut te) != 0;
        while ok {
            if te.th32OwnerProcessID == pid {
                let th = OpenThread(THREAD_SUSPEND_RESUME, 0, te.th32ThreadID);
                if !th.is_null() {
                    ResumeThread(th);
                    CloseHandle(th);
                }
            }
            ok = Thread32Next(snap, &mut te) != 0;
        }
        CloseHandle(snap);
    }
}

#[derive(Clone, Debug)]
pub struct ProcInfo {
    pub pid: u32,
    pub ppid: u32,
    pub exe: String,
}

pub fn snapshot() -> Vec<ProcInfo> {
    let mut out = Vec::new();
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return out;
        }
        let mut pe: PROCESSENTRY32W = std::mem::zeroed();
        pe.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snap, &mut pe) != 0;
        while ok {
            let len = pe.szExeFile.iter().position(|&c| c == 0).unwrap_or(pe.szExeFile.len());
            out.push(ProcInfo { pid: pe.th32ProcessID, ppid: pe.th32ParentProcessID, exe: String::from_utf16_lossy(&pe.szExeFile[..len]) });
            ok = Process32NextW(snap, &mut pe) != 0;
        }
        CloseHandle(snap);
    }
    out
}

fn creation_time(pid: u32) -> Option<u64> {
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return None;
        }
        let mut c: FILETIME = std::mem::zeroed();
        let mut e: FILETIME = std::mem::zeroed();
        let mut k: FILETIME = std::mem::zeroed();
        let mut u: FILETIME = std::mem::zeroed();
        let ok = GetProcessTimes(h, &mut c, &mut e, &mut k, &mut u) != 0;
        CloseHandle(h);
        ok.then(|| ((c.dwHighDateTime as u64) << 32) | c.dwLowDateTime as u64)
    }
}

pub fn image_path(pid: u32) -> Option<String> {
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return None;
        }
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, buf.as_mut_ptr(), &mut len) != 0;
        CloseHandle(h);
        ok.then(|| String::from_utf16_lossy(&buf[..len as usize]))
    }
}

pub fn is_alive(pid: u32) -> bool {
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return false;
        }
        let mut code = 0u32;
        let ok = GetExitCodeProcess(h, &mut code) != 0;
        CloseHandle(h);
        ok && code == 259 // STILL_ACTIVE
    }
}

/// `roots` plus every descendant (by parent pid). A child only counts if it was created after
/// its parent — guards against a recycled parent pid adopting strangers.
pub fn tree(roots: &[u32]) -> Vec<ProcInfo> {
    let all = snapshot();
    let by_pid: HashMap<u32, ProcInfo> = all.iter().map(|p| (p.pid, p.clone())).collect();
    let mut kids: HashMap<u32, Vec<u32>> = HashMap::new();
    for p in &all {
        kids.entry(p.ppid).or_default().push(p.pid);
    }
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    let mut q: VecDeque<u32> = roots.iter().copied().collect();
    while let Some(pid) = q.pop_front() {
        if !seen.insert(pid) {
            continue;
        }
        let Some(info) = by_pid.get(&pid) else { continue };
        out.push(info.clone());
        let parent_t = creation_time(pid);
        for &k in kids.get(&pid).map(Vec::as_slice).unwrap_or(&[]) {
            if k == pid {
                continue;
            }
            let ok = match (parent_t, creation_time(k)) {
                (Some(pt), Some(kt)) => kt >= pt,
                _ => true,
            };
            if ok {
                q.push_back(k);
            }
        }
    }
    out
}

pub fn parent_of(pid: u32) -> Option<u32> {
    snapshot().into_iter().find(|p| p.pid == pid).map(|p| p.ppid)
}

pub fn terminate(pid: u32) -> bool {
    unsafe {
        let h = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if h.is_null() {
            return false;
        }
        let ok = TerminateProcess(h, 1) != 0;
        CloseHandle(h);
        ok
    }
}

/// Whether two Windows paths name the same file as far as text goes: case, `/` vs `\` and a
/// `\\?\` prefix do not matter. (Used to recognise the venv launcher above an adopted service.)
pub fn same_path(a: &str, b: &str) -> bool {
    norm(a) == norm(b)
}

fn norm(p: &str) -> String {
    let p = p.trim().trim_matches('"').replace('/', "\\");
    let p = p.strip_prefix(r"\\?\").unwrap_or(&p);
    p.trim_end_matches('\\').to_lowercase()
}

/// Open a folder in Explorer.
pub fn open_folder(dir: &Path) -> std::io::Result<()> {
    Command::new("explorer.exe").arg(dir).spawn().map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_compare_like_windows_does() {
        assert!(same_path(r"C:\Repo\mcp\.venv\Scripts\pythonw.exe", r"c:/repo/MCP/.venv/scripts/PYTHONW.EXE"));
        assert!(same_path(r"\\?\C:\a\b.exe", r"C:\a\b.exe"));
        assert!(same_path(r#""C:\a\b\""#, r"C:\a\b"));
        assert!(!same_path(r"C:\a\pythonw.exe", r"C:\b\pythonw.exe"));
    }

    #[test]
    fn job_tree_and_terminate_on_a_real_child() {
        // ping waits ~5 s: long enough to look at it, short enough not to linger if the test fails
        let job = Job::new().expect("job");
        let mut cmd = Command::new("cmd.exe");
        cmd.args(["/c", "ping -n 6 127.0.0.1 >nul"]);
        let (mut child, in_job) = spawn_in_job(&mut cmd, Some(&job)).unwrap();
        assert!(in_job);
        let pid = child.id();
        std::thread::sleep(std::time::Duration::from_millis(500));
        let t = tree(&[pid]);
        assert!(t.iter().any(|p| p.exe.eq_ignore_ascii_case("ping.exe")), "{t:?}");
        assert!(image_path(pid).unwrap().to_lowercase().ends_with("cmd.exe"));
        assert!(job.terminate());
        let _ = child.wait();
        std::thread::sleep(std::time::Duration::from_millis(300));
        assert!(t.iter().all(|p| !is_alive(p.pid)), "the whole tree went with the job");
    }
}
