use super::Shortcut;
use std::{
    cell::RefCell,
    fs,
    io::{self, Read, Write},
    path::Path,
    ptr,
};
use tauri::Emitter;
use windows_sys::Win32::{
    Foundation::{GetLastError, ERROR_CLASS_ALREADY_EXISTS, HWND, LPARAM, LRESULT, WPARAM},
    System::LibraryLoader::GetModuleHandleW,
    UI::{
        Input::KeyboardAndMouse::{RegisterHotKey, UnregisterHotKey, MOD_NOREPEAT},
        WindowsAndMessaging::{
            CreateWindowExW, DefWindowProcW, DestroyWindow, GetForegroundWindow, RegisterClassW,
            HWND_MESSAGE, WM_HOTKEY, WNDCLASSW,
        },
    },
};

const FILE: &str = "pet-shortcut";
const DEFAULT: &str = "Ctrl+Shift+H";
const UNAVAILABLE: &str = "pet_hotkey_unavailable";
type Outcome<T> = Result<T, &'static str>;

thread_local! {
    static HOST: RefCell<Option<Host>> = const { RefCell::new(None) };
}

struct Host {
    window: HWND,
    chord: Option<Shortcut>,
    id: Option<i32>,
    capture: Option<(isize, tauri::WebviewWindow)>,
}

fn read_preference(root: &Path) -> Option<Shortcut> {
    let mut text = String::new();
    match fs::File::open(root.join(FILE)).and_then(|file| file.take(49).read_to_string(&mut text)) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            Shortcut::parse(DEFAULT).expect("default shortcut")
        }
        Ok(_) => Shortcut::parse(&text).unwrap_or(None),
        Err(_) => None,
    }
}

unsafe extern "system" fn receive(hwnd: HWND, message: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    #[cfg(feature = "patrol-worker")]
    if crate::patrol_win32::event(message, w) {
        return 0;
    }
    if message == WM_HOTKEY {
        let current = HOST
            .try_with(|slot| {
                let slot = slot.try_borrow().ok()?;
                let host = slot.as_ref()?;
                let chord = host.chord.as_ref()?;
                // An old message may still be queued after a rebind.
                let wire = l as u32;
                if host.window != hwnd
                    || host.id != Some(w as i32)
                    || (wire & 0xf) != chord.modifiers
                    || (wire >> 16) != chord.key
                {
                    return None;
                }
                Some(
                    host.capture
                        .as_ref()
                        .filter(|(owner, _)| *owner == unsafe { GetForegroundWindow() } as isize)
                        .map(|(_, window)| window.clone()),
                )
            })
            .ok()
            .flatten();
        match current {
            Some(Some(window)) => {
                let _ = window.emit("pet-hotkey-current", ());
            }
            Some(None) => crate::pet_win32::toggle_compact(),
            None => (),
        }
        return 0;
    }
    unsafe { DefWindowProcW(hwnd, message, w, l) }
}

pub(crate) fn start(root: &Path) -> Outcome<()> {
    let class: Vec<u16> = "MoeGuardPetHotkey\0".encode_utf16().collect();
    let window = unsafe {
        let instance = GetModuleHandleW(ptr::null());
        if instance.is_null() {
            return Err(UNAVAILABLE);
        }
        let descriptor = WNDCLASSW {
            lpfnWndProc: Some(receive),
            hInstance: instance,
            lpszClassName: class.as_ptr(),
            ..Default::default()
        };
        if RegisterClassW(&descriptor) == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS {
            return Err(UNAVAILABLE);
        }
        CreateWindowExW(
            0,
            class.as_ptr(),
            ptr::null(),
            0,
            0,
            0,
            0,
            0,
            HWND_MESSAGE,
            ptr::null_mut(),
            instance,
            ptr::null(),
        )
    };
    if window.is_null() {
        return Err(UNAVAILABLE);
    }
    let chord = read_preference(root);
    let id = chord
        .as_ref()
        .filter(|chord| register(window, 1, chord))
        .map(|_| 1);
    HOST.with(|slot| {
        slot.replace(Some(Host {
            window,
            chord,
            id,
            capture: None,
        }))
    });
    #[cfg(feature = "patrol-worker")]
    crate::patrol_win32::register(window);
    Ok(())
}

pub(crate) fn capture(window: Option<tauri::WebviewWindow>) -> Outcome<()> {
    let recording = match window {
        Some(window) => Some((window.hwnd().map_err(|_| UNAVAILABLE)?.0 as isize, window)),
        None => None,
    };
    HOST.with(|slot| {
        let mut slot = slot.borrow_mut();
        slot.as_mut().ok_or(UNAVAILABLE)?.capture = recording;
        Ok(())
    })
}

fn register(window: HWND, id: i32, chord: &Shortcut) -> bool {
    unsafe { RegisterHotKey(window, id, chord.modifiers | MOD_NOREPEAT, chord.key) != 0 }
}

pub(crate) fn read() -> Outcome<(String, bool)> {
    HOST.with(|slot| {
        let host = slot.borrow();
        let host = host.as_ref().ok_or(UNAVAILABLE)?;
        Ok((
            host.chord
                .as_ref()
                .map_or_else(String::new, |key| key.label.clone()),
            host.id.is_some(),
        ))
    })
}

fn persist(root: &Path, text: &str) -> io::Result<()> {
    fs::create_dir_all(root)?;
    let mut temporary = tempfile::NamedTempFile::new_in(root)?;
    temporary.write_all(text.as_bytes())?;
    temporary.as_file().sync_all()?;
    temporary
        .persist(root.join(FILE))
        .map(|_| ())
        .map_err(|err| err.error)
}

pub(crate) fn save(root: &Path, text: &str) -> Outcome<(String, bool)> {
    let next = Shortcut::parse(text)?;
    let result = HOST.with(|slot| {
        let mut slot = slot.borrow_mut();
        let host = slot.as_mut().ok_or(UNAVAILABLE)?;
        if next == host.chord && host.id.is_some() {
            return Ok((
                host.chord.as_ref().expect("registered chord").label.clone(),
                true,
            ));
        }
        let id = if host.id == Some(1) { 2 } else { 1 };
        let new_id = match next.as_ref() {
            Some(chord) if register(host.window, id, chord) => Some(id),
            Some(_) => return Err("pet_hotkey_conflict"),
            None => None,
        };
        let value = next.as_ref().map_or("", |key| key.label.as_str());
        if persist(root, value).is_err() {
            if let Some(id) = new_id {
                unsafe {
                    UnregisterHotKey(host.window, id);
                }
            }
            return Err("pet_hotkey_save_failed");
        }
        if let Some(id) = host.id {
            unsafe {
                UnregisterHotKey(host.window, id);
            }
        }
        let label = value.to_owned();
        host.chord = next;
        host.id = new_id;
        Ok((label, new_id.is_some()))
    });
    if result.as_ref().is_ok_and(|(label, _)| label.is_empty()) {
        crate::pet_win32::exit_compact();
    }
    result
}

pub(crate) fn stop() {
    if let Some(host) = HOST.with(|slot| slot.take()) {
        #[cfg(feature = "patrol-worker")]
        crate::patrol_win32::unregister(host.window);
        unsafe {
            if let Some(id) = host.id {
                UnregisterHotKey(host.window, id);
            }
            DestroyWindow(host.window);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "requires a disposable Windows desktop and an unused Ctrl+Alt+Shift+F11"]
    fn registered_shortcut_is_released_on_disable_and_reloaded_on_start() {
        const CHORD: &str = "Ctrl+Alt+Shift+F11";
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        fs::write(root.join(FILE), "").unwrap();
        start(root).unwrap();
        let (label, active) = save(root, CHORD).unwrap();
        assert_eq!((label.as_str(), active), (CHORD, true));
        stop();
        start(root).unwrap();
        assert_eq!(read().unwrap(), (CHORD.into(), true));
        assert_eq!(save(root, "").unwrap(), (String::new(), false));
        assert_eq!(fs::read(root.join(FILE)).unwrap(), b"");
        stop();
    }
}
