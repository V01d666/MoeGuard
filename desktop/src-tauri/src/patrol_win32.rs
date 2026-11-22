//! The native host owns the indicator, while the packaged Qt core owns capture.
#[path = "patrol_indicator.rs"]
mod indicator;

use indicator::Indicator;
use serde_json::{json, Value};
use std::{
    cell::{Cell, RefCell},
    io::{BufRead, BufReader, Read, Write},
    mem::size_of,
    os::windows::{io::AsRawHandle, process::CommandExt},
    path::PathBuf,
    process::{Child, Command, Stdio},
    ptr::null,
    sync::mpsc::{self, Receiver, TryRecvError},
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, HANDLE, HWND},
    System::{
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        RemoteDesktop::{
            WTSRegisterSessionNotification, WTSUnRegisterSessionNotification,
            NOTIFY_FOR_THIS_SESSION,
        },
        Threading::CREATE_NO_WINDOW,
    },
    UI::WindowsAndMessaging::{
        KillTimer, SetTimer, WM_TIMER, WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK,
    },
};

const STOP_GRACE: Duration = Duration::from_secs(4);
const WATCH: usize = 7121;
thread_local! {
    static RUN: RefCell<Option<Patrol>> = const { RefCell::new(None) };
    static WATCHING: Cell<bool> = const { Cell::new(false) };
    static SESSION_NOTIFICATIONS: Cell<bool> = const { Cell::new(false) };
    static LOCKED: Cell<bool> = const { Cell::new(false) };
    static LOCK_PENDING: Cell<bool> = const { Cell::new(false) };
    static SERIAL: Cell<u32> = const { Cell::new(0) };
    static LAST: RefCell<Option<(u32, Value)>> = const { RefCell::new(None) };
}

struct Setup {
    id: u32,
    receive: Receiver<Value>,
    preview: Option<Value>,
    reply: Option<Value>,
    enrollment: bool,
    deadline: Instant,
}

struct Patrol {
    child: Child,
    job: HANDLE,
    indicator: Option<Indicator>,
    stopping: Option<Instant>,
    setup: Option<Setup>,
}

impl Patrol {
    fn stop(&mut self) {
        self.child.stdin.take();
        if let Some(setup) = &mut self.setup {
            setup.preview = None;
        }
        self.stopping.get_or_insert_with(Instant::now);
    }
}

impl Drop for Patrol {
    fn drop(&mut self) {
        self.stop();
        // Closing the job kills a stuck worker before removing the indicator.
        unsafe { CloseHandle(self.job) };
        let _ = self.child.wait();
    }
}

fn helper() -> Option<PathBuf> {
    let path = std::env::current_exe()
        .ok()?
        .parent()?
        .join("MoeGuard-Patrol")
        .join("MoeGuard-Patrol.exe");
    path.is_file().then_some(path)
}

pub(crate) fn available() -> bool {
    helper().is_some()
}

pub(crate) fn register(hwnd: HWND) {
    if available() && unsafe { SetTimer(hwnd, WATCH, 100, None) } != 0 {
        WATCHING.set(true);
        SESSION_NOTIFICATIONS
            .set(unsafe { WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION) } != 0);
    }
}

pub(crate) fn unregister(hwnd: HWND) {
    if SESSION_NOTIFICATIONS.replace(false) {
        unsafe { WTSUnRegisterSessionNotification(hwnd) };
    }
    if WATCHING.replace(false) {
        unsafe { KillTimer(hwnd, WATCH) };
    }
}

pub(crate) fn event(message: u32, w: usize) -> bool {
    if message == WM_TIMER && w == WATCH {
        tick();
    } else if message == WM_WTSSESSION_CHANGE {
        match w as u32 {
            WTS_SESSION_LOCK => {
                LOCKED.set(true);
                cancel_setup();
                if RUN.with(|slot| {
                    slot.borrow()
                        .as_ref()
                        .is_some_and(|worker| worker.setup.is_some())
                }) {
                    LOCK_PENDING.set(true);
                } else {
                    start(true);
                }
            }
            WTS_SESSION_UNLOCK => {
                LOCKED.set(false);
                stop();
            }
            _ => return false,
        }
    } else {
        return false;
    }
    true
}

pub(crate) fn update_language(app: &tauri::AppHandle, language: &crate::ui_locale::UiLocale) {
    let labels = [
        language.message("patrol.cameraActive"),
        language.message("patrol.cameraPreview"),
    ];
    let _ = app.run_on_main_thread(move || {
        indicator::set_labels(labels);
        RUN.with(|slot| {
            if let Some(indicator) = slot
                .borrow()
                .as_ref()
                .and_then(|patrol| patrol.indicator.as_ref())
            {
                indicator.refresh_title();
            }
        });
    });
}

fn launch(
    trigger: &[u8],
    setup: Option<(u32, bool)>,
    needs_setup: Option<Box<dyn FnOnce() + Send>>,
) -> Option<Patrol> {
    let path = helper()?;
    let job = unsafe { CreateJobObjectW(null(), null()) };
    if job.is_null() {
        return None;
    }
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if unsafe {
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    } == 0
    {
        unsafe { CloseHandle(job) };
        return None;
    }
    let indicator = if setup.is_none_or(|(_, enrollment)| enrollment) {
        let Some(indicator) = Indicator::show(setup.is_some()) else {
            unsafe { CloseHandle(job) };
            return None;
        };
        Some(indicator)
    } else {
        None
    };
    let mut command = Command::new(path);
    if let Some((_, enrollment)) = setup {
        command
            .arg(if enrollment { "--enroll" } else { "--settings" })
            .stdout(Stdio::piped());
    } else if needs_setup.is_some() {
        command.stdout(Stdio::piped());
    } else {
        command.stdout(Stdio::null());
    }
    let Ok(mut child) = command
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        unsafe { CloseHandle(job) };
        return None;
    };
    if unsafe { AssignProcessToJobObject(job, child.as_raw_handle()) } == 0 {
        let _ = child.kill();
        let _ = child.wait();
        unsafe { CloseHandle(job) };
        return None;
    }
    let mut patrol = Patrol {
        child,
        job,
        indicator,
        stopping: None,
        setup: None,
    };
    if let Some((id, enrollment)) = setup {
        let reader = BufReader::new(patrol.child.stdout.take()?).take(160 * 1024);
        let (send, receive) = mpsc::sync_channel(2);
        std::thread::spawn(move || {
            for line in BufReader::new(reader).lines().take(2).map_while(Result::ok) {
                if let Ok(value) = serde_json::from_str::<Value>(&line) {
                    let _ = send.try_send(value);
                }
            }
        });
        patrol.setup = Some(Setup {
            id,
            receive,
            preview: None,
            reply: None,
            enrollment,
            deadline: Instant::now() + Duration::from_secs(if enrollment { 90 } else { 30 }),
        });
    }
    if let Some(needs_setup) = needs_setup {
        let stdout = patrol.child.stdout.take()?;
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut first = String::new();
            if (&mut reader).take(64).read_line(&mut first).is_ok()
                && first.trim_end() == "setup_required"
            {
                needs_setup();
            }
            let _ = std::io::copy(&mut reader, &mut std::io::sink());
        });
    }
    patrol.child.stdin.as_mut()?.write_all(trigger).ok()?;
    if setup.is_some_and(|(_, enrollment)| !enrollment) {
        patrol.child.stdin.take();
    }
    Some(patrol)
}

pub(crate) fn tick() {
    RUN.with(|slot| {
        let mut slot = slot.borrow_mut();
        if slot.as_mut().is_some_and(|patrol| {
            if patrol
                .indicator
                .as_ref()
                .is_some_and(|indicator| !indicator.is_visible())
            {
                patrol.stop();
            }
            let done = !matches!(patrol.child.try_wait(), Ok(None));
            let stopped = patrol
                .stopping
                .is_some_and(|since| since.elapsed() >= STOP_GRACE);
            let Some(setup) = &mut patrol.setup else {
                return done || stopped;
            };
            let drained = loop {
                match setup.receive.try_recv() {
                    Ok(value) if setup.enrollment && value["preview"].is_string() => {
                        if patrol.stopping.is_none() {
                            setup.preview = Some(value);
                        }
                    }
                    Ok(value) => setup.reply = Some(value),
                    Err(error) => break error == TryRecvError::Disconnected,
                }
            };
            if (done && drained) || stopped || Instant::now() >= setup.deadline {
                let reply = setup
                    .reply
                    .take()
                    .unwrap_or_else(|| json!({"error":"settings_failed"}));
                LAST.replace(Some((setup.id, reply)));
                true
            } else {
                false
            }
        }) {
            slot.take();
        }
    });
    if LOCK_PENDING.get() && RUN.with(|slot| slot.borrow().is_none()) {
        LOCK_PENDING.set(false);
        if LOCKED.get() {
            start(true);
        }
    }
}

pub(crate) fn start(locked: bool) {
    start_with_setup(locked, None);
}

pub(crate) fn start_with_setup(locked: bool, needs_setup: Option<Box<dyn FnOnce() + Send>>) {
    if !WATCHING.get() || !crate::pet_win32::patrol_allowed() {
        return;
    }
    RUN.with(|slot| {
        if slot.borrow().is_none() {
            slot.replace(launch(
                if locked { b"lock\n" } else { b"manual\n" },
                None,
                needs_setup,
            ));
        }
    });
}

pub(crate) fn stop() {
    LOCK_PENDING.set(false);
    RUN.with(|slot| {
        if let Some(patrol) = slot.borrow_mut().as_mut() {
            if patrol.setup.as_ref().is_none_or(|setup| setup.enrollment) {
                patrol.stop();
            }
        }
    });
}

pub(crate) fn setup(request: Value) -> Result<Value, &'static str> {
    if !available() {
        return Err("patrol_component_missing");
    }
    if !WATCHING.get() {
        return Err("patrol_unavailable");
    }
    let op = request["op"].as_str().ok_or("invalid_settings")?;
    let id = request["id"].as_u64().and_then(|v| u32::try_from(v).ok());
    tick();
    if op == "status" {
        return Ok(json!({"busy":RUN.with(|slot| slot.borrow().is_some())}));
    }
    if op == "poll" {
        if let Some((_, reply)) =
            LAST.with(|last| last.borrow_mut().take_if(|entry| Some(entry.0) == id))
        {
            return Ok(reply);
        }
    }
    RUN.with(|slot| {
        let mut slot = slot.borrow_mut();
        if matches!(op, "poll" | "register" | "cancel") {
            if let Some(patrol) = slot.as_mut() {
                let state = patrol
                    .setup
                    .as_mut()
                    .filter(|state| Some(state.id) == id)
                    .ok_or("patrol_busy")?;
                if op == "cancel" && state.enrollment {
                    patrol.stop();
                    return Ok(Value::Null);
                }
                if op == "register" {
                    if !state.enrollment
                        || patrol.stopping.is_some()
                        || state.preview.take().is_none()
                    {
                        return Err("settings_failed");
                    }
                    patrol
                        .child
                        .stdin
                        .take()
                        .ok_or("settings_failed")?
                        .write_all(b"save\n")
                        .map_err(|_| "settings_failed")?;
                }
                return Ok(state
                    .preview
                    .clone()
                    .unwrap_or_else(|| json!({"pending":true})));
            }
            return Err("settings_failed");
        }
        if slot.is_some() {
            return Err("patrol_busy");
        }
        let enrollment = op == "enroll";
        if enrollment && (LOCKED.get() || !crate::pet_win32::patrol_allowed()) {
            return Err("patrol_busy");
        }
        let data = if enrollment {
            "preview\n".to_owned()
        } else {
            if !matches!(
                op,
                "view"
                    | "consent"
                    | "save"
                    | "withdraw"
                    | "evidence"
                    | "evidence_open"
                    | "evidence_delete"
                    | "evidence_delete_all"
            ) {
                return Err("invalid_settings");
            }
            request.to_string() + "\n"
        };
        if data.len() > 4096 {
            return Err("invalid_settings");
        }
        let id = SERIAL.get().wrapping_add(1);
        SERIAL.set(id);
        LAST.replace(None);
        *slot =
            Some(launch(data.as_bytes(), Some((id, enrollment)), None).ok_or("settings_failed")?);
        Ok(json!({"id":id}))
    })
}

pub(crate) fn cancel_setup() {
    RUN.with(|slot| {
        if let Some(worker) = slot.borrow_mut().as_mut() {
            if worker.setup.as_ref().is_some_and(|state| state.enrollment) {
                worker.stop();
            }
        }
    });
    LAST.replace(None);
}

pub(crate) fn open_evidence(leaf: Option<&str>) -> Result<(), &'static str> {
    use std::os::windows::fs::MetadataExt;
    const UNAVAILABLE: &str = "evidence_unavailable";
    let home = crate::instance_guard::legacy_home().map_err(|_| UNAVAILABLE)?;
    let mut path = home.join(".moeguard");
    let safe = |path: &std::path::Path| {
        std::fs::symlink_metadata(path).is_ok_and(|metadata| {
            metadata.is_dir() && metadata.file_attributes() & 0x400 == 0 // reparse point
        })
    };
    if !safe(&path) {
        return Err(UNAVAILABLE);
    }
    path.push("evidence");
    if !safe(&path) {
        return Err(UNAVAILABLE);
    }
    if let Some(leaf) = leaf {
        if leaf.len() < 15
            || leaf.len() > 56
            || !leaf
                .bytes()
                .all(|b| b.is_ascii_hexdigit() || b == b'_' || b == b'-')
        {
            return Err(UNAVAILABLE);
        }
        path.push(leaf);
        if !safe(&path) {
            return Err(UNAVAILABLE);
        }
    }
    crate::system_folder::open_in_file_manager(&path).map_err(|_| UNAVAILABLE)
}

pub(crate) fn shutdown() {
    stop();
    let until = Instant::now() + STOP_GRACE;
    loop {
        tick();
        if !RUN.with(|slot| slot.borrow().is_some()) || Instant::now() >= until {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    RUN.with(|slot| {
        slot.take();
    });
}
