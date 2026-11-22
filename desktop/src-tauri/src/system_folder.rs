use std::{io, path::Path};

pub(crate) fn open_redemption_page() -> io::Result<()> {
    open_in_file_manager(Path::new("https://ifdian.net/a/moeguard"))
}

pub(crate) fn open_project_repository() -> io::Result<()> {
    open_in_file_manager(Path::new("https://github.com/V01d666/MoeGuard"))
}

#[cfg(windows)]
pub(crate) fn open_in_file_manager(path: &Path) -> io::Result<()> {
    use std::{os::windows::ffi::OsStrExt, ptr::null, ptr::null_mut};
    use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};

    let path: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    let verb = [b'o' as u16, b'p' as u16, b'e' as u16, b'n' as u16, 0];
    let result = unsafe {
        ShellExecuteW(
            null_mut(),
            verb.as_ptr(),
            path.as_ptr(),
            null(),
            null(),
            SW_SHOWNORMAL,
        )
    };
    if result as isize > 32 {
        Ok(())
    } else {
        Err(io::Error::other("Windows could not open the target"))
    }
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn open_in_file_manager(path: &Path) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    let mut command = {
        let mut command = std::process::Command::new("gio");
        command.arg("open");
        command
    };
    #[cfg(target_os = "macos")]
    let mut command = std::process::Command::new("/usr/bin/open");
    if command.arg("--").arg(path).status()?.success() {
        Ok(())
    } else {
        Err(io::Error::other("file manager rejected the data directory"))
    }
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn open_in_file_manager(_path: &Path) -> io::Result<()> {
    Err(io::ErrorKind::Unsupported.into())
}
