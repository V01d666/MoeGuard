//! Lock and atomically refresh an existing legacy session; never create an identity.
use super::{
    parse_payload, read_file, read_payload, session_from, session_path, validate, Envelope, Result,
    Session, MAX_BYTES, STANDARD,
};
use base64::Engine as _;
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
};
use zeroize::Zeroizing;

pub(crate) type Protector = fn(&[u8]) -> Result<Zeroizing<Vec<u8>>>;
type Outcome<T> = std::result::Result<T, &'static str>;
const UNAVAILABLE: &str = "revision_session_unavailable";
const INVALID: &str = "revision_session_invalid";
const MISSING: &str = "revision_session_missing";

pub(crate) struct Pending {
    path: PathBuf,
    original: Vec<u8>,
    data: Zeroizing<Vec<u8>>,
    _lock: File,
}

fn check_marker(path: &Path) -> Outcome<()> {
    match fs::symlink_metadata(path.with_file_name("automatic-connection-disabled")) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Ok(_) => Err("revision_session_disabled"),
        Err(_) => Err(UNAVAILABLE),
    }
}

pub(crate) fn automatic_allowed(home: &Path) -> Outcome<()> {
    check_marker(&session_path(home).map_err(|_| UNAVAILABLE)?)
}

impl Pending {
    pub(crate) fn open(home: &Path, unprotect: Protector) -> Outcome<Self> {
        let path = session_path(home).map_err(|_| UNAVAILABLE)?;
        check_marker(&path)?;
        let lock_path = path.with_file_name("role-service-enrollment.json.lock");
        if lock_path.is_symlink() {
            return Err(INVALID);
        }
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
        let lock = options.open(&lock_path).map_err(|_| UNAVAILABLE)?;
        let meta = lock.metadata().map_err(|_| UNAVAILABLE)?;
        if lock_path.is_symlink() || !meta.is_file() {
            return Err(INVALID);
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
            if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(INVALID);
            }
        }
        // On Windows std locks a range including byte zero, which the old msvcrt
        // writer locks. On Unix both use flock. The shared file is never truncated.
        match lock.try_lock() {
            Ok(()) => (),
            Err(std::fs::TryLockError::WouldBlock) => return Err("revision_session_busy"),
            Err(_) => return Err(UNAVAILABLE),
        }
        check_marker(&path)?;
        let original = read_file(&path).map_err(|_| INVALID)?.ok_or(MISSING)?;
        let data = read_payload(&original, unprotect).map_err(|_| INVALID)?;
        parse_payload(&data).map_err(|_| INVALID)?;
        Ok(Self {
            path,
            original,
            data,
            _lock: lock,
        })
    }

    pub(crate) fn session(&self, now: u64) -> Outcome<(Session, bool)> {
        let raw = parse_payload(&self.data).map_err(|_| INVALID)?;
        Ok((session_from(&raw), raw.expires_at <= now))
    }

    pub(crate) fn request_body(&self) -> Outcome<Zeroizing<Vec<u8>>> {
        let raw = parse_payload(&self.data).map_err(|_| INVALID)?;
        #[derive(serde::Serialize)]
        struct Request<'a> {
            schema_version: u8,
            enrollment_id: &'a str,
            enrollment_secret: &'a str,
            renew_account_id: &'a str,
        }
        serde_json::to_vec(&Request {
            schema_version: 1,
            enrollment_id: raw.enrollment_id,
            enrollment_secret: raw.enrollment_secret,
            renew_account_id: raw.account_id,
        })
        .map(Zeroizing::new)
        .map_err(|_| INVALID)
    }

    pub(crate) fn commit(
        self,
        account: &str,
        bearer: &str,
        expires: u64,
        now: u64,
        protect: Protector,
    ) -> Outcome<Session> {
        let mut raw = parse_payload(&self.data).map_err(|_| INVALID)?;
        if account != raw.account_id
            || bearer == raw.bearer_token
            || expires <= now
            || expires - now > 366 * 24 * 60 * 60
        {
            return Err(INVALID);
        }
        raw.bearer_token = bearer;
        raw.expires_at = expires;
        validate(&raw).map_err(|_| INVALID)?;
        let plain = Zeroizing::new(serde_json::to_vec(&raw).map_err(|_| INVALID)?);
        let ciphertext = protect(&plain).map_err(|_| UNAVAILABLE)?;
        let encoded = STANDARD.encode(&*ciphertext);
        let wire = serde_json::to_vec(&Envelope {
            schema_version: 1,
            protection: "windows-dpapi-current-user",
            payload: &encoded,
        })
        .map_err(|_| UNAVAILABLE)?;
        if ciphertext.is_empty() || wire.len() > MAX_BYTES {
            return Err(UNAVAILABLE);
        }
        let mut tmp = tempfile::NamedTempFile::new_in(self.path.parent().ok_or(UNAVAILABLE)?)
            .map_err(|_| UNAVAILABLE)?;
        tmp.write_all(&wire)
            .and_then(|_| tmp.as_file().sync_all())
            .map_err(|_| UNAVAILABLE)?;
        check_marker(&self.path)?;
        if read_file(&self.path).map_err(|_| INVALID)?.as_deref() != Some(self.original.as_slice())
        {
            return Err(UNAVAILABLE);
        }
        tmp.persist(&self.path).map_err(|_| UNAVAILABLE)?;
        Ok(session_from(&raw))
    }
}
