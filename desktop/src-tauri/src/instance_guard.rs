//! Own the same process-lifetime lock as the preceding client before touching its data.
use std::{
    fs::{self, File, OpenOptions, TryLockError},
    io,
    path::Path,
};

#[cfg(all(windows, feature = "legacy-workshop"))]
pub(crate) fn legacy_home() -> io::Result<std::path::PathBuf> {
    // Match Python Path.home() on Windows: USERPROFILE, then HOMEDRIVE + HOMEPATH.
    let home = std::env::var_os("USERPROFILE").or_else(|| {
        let tail = std::env::var_os("HOMEPATH")?;
        let mut drive = std::env::var_os("HOMEDRIVE").unwrap_or_default();
        drive.push(tail);
        Some(drive)
    });
    home.map(std::path::PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or_else(|| io::Error::other("legacy profile path unavailable"))
}

fn is_link(meta: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
        meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    meta.file_type().is_symlink()
}

pub(crate) fn acquire(home: &Path) -> io::Result<Option<File>> {
    let root = home.join(".moeguard");
    match fs::create_dir(&root) {
        Ok(()) => (),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error),
    }
    let directory = fs::symlink_metadata(&root)?;
    if !directory.is_dir() || is_link(&directory) {
        return Err(io::Error::other("invalid application lock directory"));
    }
    let path = root.join("application.lock");
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    match fs::symlink_metadata(&path) {
        Ok(meta) if !meta.is_file() || is_link(&meta) => {
            return Err(io::Error::other("invalid application lock file"));
        }
        Ok(_) => (),
        Err(error) if error.kind() == io::ErrorKind::NotFound => (),
        Err(error) => return Err(error),
    }
    let lock = options.open(path)?;
    let meta = lock.metadata()?;
    if !meta.is_file() || is_link(&meta) {
        return Err(io::Error::other("invalid application lock file"));
    }
    // Rust locks a range that includes byte zero on Windows (msvcrt), and
    // uses flock on Linux. Dropping the descriptor releases the lock.
    match lock.try_lock() {
        Ok(()) => Ok(Some(lock)),
        Err(TryLockError::WouldBlock) => Ok(None),
        Err(TryLockError::Error(error)) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::acquire;
    use std::{fs, io};

    #[test]
    fn guard_contends_and_releases_without_changing_lock_file() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(".moeguard/application.lock");
        fs::create_dir(path.parent().unwrap()).unwrap();
        fs::write(&path, b"keep these bytes").unwrap();
        let first = acquire(home.path()).unwrap().unwrap();
        assert!(acquire(home.path()).unwrap().is_none());
        drop(first);
        assert_eq!(fs::read(&path).unwrap(), b"keep these bytes");
        assert!(acquire(home.path()).unwrap().is_some());
    }

    #[test]
    fn abnormal_lock_location_is_not_treated_as_busy() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join(".moeguard/application.lock")).unwrap();
        assert_ne!(
            acquire(home.path()).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    // Launched in an isolated profile by the Python compatibility probe.
    #[test]
    #[ignore = "requires a disposable profile and Python FileLock"]
    fn legacy_instance_lock_driver() {
        let home = std::path::PathBuf::from(std::env::var_os("MG_INSTANCE_TEST_HOME").unwrap());
        let guard = acquire(&home).unwrap();
        match std::env::var("MG_INSTANCE_TEST_EXPECT").unwrap().as_str() {
            "busy" => assert!(guard.is_none()),
            "acquired" => {
                assert!(guard.is_some());
                fs::write(home.join("rust-ready"), b"acquired").unwrap();
                let release = home.join("rust-release");
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
                while !release.exists() {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "fixture release timed out"
                    );
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
            }
            _ => panic!("unsupported instance fixture"),
        }
        drop(guard);
    }
}
