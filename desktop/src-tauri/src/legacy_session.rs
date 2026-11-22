//! Native adapter for the shipped Windows role-service session. Not a WebView API.
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::Deserialize;
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read},
    path::Path,
};
use zeroize::Zeroizing;

const MAX_BYTES: usize = 64 * 1024;
const SESSION_PATH: &[&str] = &[
    ".moeguard",
    "role-workbench",
    "service-client",
    "role-service-session.json",
];
const ORIGIN_CONFIG: &str = include_str!("../../../resources/role-service.json");

pub(crate) enum SessionError {
    Invalid,
    Unavailable,
}

type Result<T> = std::result::Result<T, SessionError>;

// No Debug or Serialize: credentials belong to native request construction only.
pub(crate) struct Session {
    account: String,
    bearer: Zeroizing<String>,
}

impl Session {
    pub(crate) fn account(&self) -> &str {
        &self.account
    }

    pub(crate) fn bearer(&self) -> &str {
        self.bearer.as_str()
    }

    #[cfg(any(feature = "legacy-workshop", all(test, feature = "revision-loopback")))]
    pub(crate) fn into_parts(self) -> (String, Zeroizing<String>) {
        (self.account, self.bearer)
    }
}

pub(crate) enum SessionState {
    Missing,
    Expired,
    Ready(Session),
}

#[derive(Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct Envelope<'a> {
    schema_version: u8,
    protection: &'a str,
    payload: &'a str,
}

#[derive(Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct Payload<'a> {
    schema_version: u8,
    service_origin: &'a str,
    account_id: &'a str,
    expires_at: u64,
    bearer_token: &'a str,
    enrollment_id: &'a str,
    enrollment_secret: &'a str,
}

fn read_file(path: &Path) -> Result<Option<Vec<u8>>> {
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(SessionError::Unavailable),
    };
    if !meta.is_file() || meta.len() > MAX_BYTES as u64 {
        return Err(SessionError::Invalid);
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file: File = options.open(path).map_err(|_| SessionError::Unavailable)?;
    let opened = file.metadata().map_err(|_| SessionError::Unavailable)?;
    if !opened.is_file() || opened.len() > MAX_BYTES as u64 {
        return Err(SessionError::Invalid);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
        if opened.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(SessionError::Invalid);
        }
    }
    let mut bytes = Vec::new();
    file.take((MAX_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| SessionError::Unavailable)?;
    if bytes.is_empty() || bytes.len() > MAX_BYTES {
        return Err(SessionError::Invalid);
    }
    Ok(Some(bytes))
}

fn lower_hex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn url_safe(value: &str, min: usize) -> bool {
    (min..=128).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

pub(crate) fn service_origin() -> Result<&'static str> {
    #[derive(Deserialize)]
    struct Origin<'a> {
        schema_version: u32,
        service_origin: &'a str,
    }
    let origin: Origin<'_> =
        serde_json::from_str(ORIGIN_CONFIG).map_err(|_| SessionError::Invalid)?;
    if origin.schema_version != 1 || !origin.service_origin.starts_with("https://") {
        return Err(SessionError::Invalid);
    }
    Ok(origin.service_origin)
}

fn validate(raw: &Payload<'_>) -> Result<()> {
    let origin = service_origin()?;
    let bearer = raw
        .bearer_token
        .strip_prefix("mgr_")
        .and_then(|rest| rest.split_once('_'));
    let account_byte = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit();
    if raw.schema_version != 1
        || raw
            .service_origin
            .strip_suffix('/')
            .unwrap_or(raw.service_origin)
            != origin
        || !(3..=64).contains(&raw.account_id.len())
        || !raw.account_id.bytes().next().is_some_and(account_byte)
        || !raw.account_id.bytes().all(|b| account_byte(b) || b == b'-')
        || raw.expires_at == 0
        || !bearer.is_some_and(|(id, secret)| lower_hex(id, 24) && url_safe(secret, 32))
        || !raw
            .enrollment_id
            .strip_prefix("eni_")
            .is_some_and(|id| lower_hex(id, 32))
        || !raw
            .enrollment_secret
            .strip_prefix("ens_")
            .is_some_and(|secret| url_safe(secret, 43))
    {
        return Err(SessionError::Invalid);
    }
    Ok(())
}

pub(crate) fn load_with(
    home: &Path,
    now: u64,
    unprotect: impl FnOnce(&[u8]) -> Result<Zeroizing<Vec<u8>>>,
) -> Result<SessionState> {
    let Some(data) = load_payload(home, unprotect)? else {
        return Ok(SessionState::Missing);
    };
    let raw = parse_payload(&data)?;
    if raw.expires_at <= now {
        return Ok(SessionState::Expired);
    }
    Ok(SessionState::Ready(session_from(&raw)))
}

#[cfg(all(windows, feature = "legacy-workshop"))]
pub(crate) fn stored_support_id(home: &Path) -> Result<Option<String>> {
    let Some(data) = load_payload(home, unprotect_current_user)? else {
        return Ok(None);
    };
    Ok(Some(parse_payload(&data)?.account_id.to_owned()))
}

fn load_payload(
    home: &Path,
    unprotect: impl FnOnce(&[u8]) -> Result<Zeroizing<Vec<u8>>>,
) -> Result<Option<Zeroizing<Vec<u8>>>> {
    read_file(&session_path(home)?)?
        .map(|wire| read_payload(&wire, unprotect))
        .transpose()
}

fn session_path(home: &Path) -> Result<std::path::PathBuf> {
    if !home.is_absolute() {
        return Err(SessionError::Unavailable);
    }
    Ok(home.join(SESSION_PATH.iter().collect::<std::path::PathBuf>()))
}

fn read_payload(
    wire: &[u8],
    unprotect: impl FnOnce(&[u8]) -> Result<Zeroizing<Vec<u8>>>,
) -> Result<Zeroizing<Vec<u8>>> {
    let envelope: Envelope<'_> = serde_json::from_slice(wire).map_err(|_| SessionError::Invalid)?;
    if envelope.schema_version != 1 || envelope.protection != "windows-dpapi-current-user" {
        return Err(SessionError::Invalid);
    }
    let ciphertext = STANDARD
        .decode(envelope.payload)
        .map_err(|_| SessionError::Invalid)?;
    if ciphertext.is_empty() {
        return Err(SessionError::Invalid);
    }
    let data = unprotect(&ciphertext)?;
    if data.is_empty() || data.len() > MAX_BYTES {
        return Err(SessionError::Invalid);
    }
    Ok(data)
}

fn parse_payload(data: &[u8]) -> Result<Payload<'_>> {
    let raw: Payload<'_> = serde_json::from_slice(data).map_err(|_| SessionError::Invalid)?;
    validate(&raw)?;
    Ok(raw)
}

fn session_from(raw: &Payload<'_>) -> Session {
    Session {
        account: raw.account_id.to_owned(),
        bearer: Zeroizing::new(raw.bearer_token.to_owned()),
    }
}

#[cfg(windows)]
pub(crate) fn load_current_user(home: &Path, now: u64) -> Result<SessionState> {
    load_with(home, now, unprotect_current_user)
}

#[cfg(windows)]
pub(crate) fn unprotect_current_user(ciphertext: &[u8]) -> Result<Zeroizing<Vec<u8>>> {
    dpapi(ciphertext, false)
}

#[cfg(all(windows, feature = "legacy-workshop"))]
pub(crate) fn protect_current_user(plaintext: &[u8]) -> Result<Zeroizing<Vec<u8>>> {
    dpapi(plaintext, true)
}

#[cfg(windows)]
fn dpapi(data: &[u8], protect: bool) -> Result<Zeroizing<Vec<u8>>> {
    use std::{ptr, slice};
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::Cryptography::{
            CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
        },
    };
    use zeroize::Zeroize;

    // Match the v0.5.0 writer's optional entropy and current-user protection.
    let entropy = b"MoeGuard/role-service/session/v1";
    let input = CRYPT_INTEGER_BLOB {
        cbData: u32::try_from(data.len()).map_err(|_| SessionError::Invalid)?,
        pbData: data.as_ptr().cast_mut(),
    };
    let entropy = CRYPT_INTEGER_BLOB {
        cbData: entropy.len() as u32,
        pbData: entropy.as_ptr().cast_mut(),
    };
    struct DpapiOutput(CRYPT_INTEGER_BLOB);
    impl Drop for DpapiOutput {
        fn drop(&mut self) {
            if !self.0.pbData.is_null() {
                // SAFETY: the Win32 call returns this LocalAlloc buffer and its byte count.
                unsafe {
                    slice::from_raw_parts_mut(self.0.pbData, self.0.cbData as usize).zeroize();
                    LocalFree(self.0.pbData.cast());
                }
            }
        }
    }
    let mut output = DpapiOutput(CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: ptr::null_mut(),
    });
    // SAFETY: both input blobs refer to readable live slices; no optional outputs requested.
    let ok = unsafe {
        if protect {
            CryptProtectData(
                &input,
                ptr::null(),
                &entropy,
                ptr::null(),
                ptr::null(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut output.0,
            )
        } else {
            CryptUnprotectData(
                &input,
                ptr::null_mut(),
                &entropy,
                ptr::null(),
                ptr::null(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut output.0,
            )
        }
    };
    if ok == 0
        || output.0.pbData.is_null()
        || output.0.cbData == 0
        || output.0.cbData as usize > MAX_BYTES
    {
        return Err(SessionError::Invalid);
    }
    // SAFETY: DPAPI returned a valid allocation on success. Copy into a zeroizing Rust buffer.
    let data = unsafe { slice::from_raw_parts(output.0.pbData, output.0.cbData as usize) };
    Ok(Zeroizing::new(data.to_vec()))
}

#[cfg(any(
    all(windows, feature = "legacy-workshop"),
    all(test, feature = "revision-loopback")
))]
#[path = "legacy_renewal.rs"]
pub(crate) mod renewal;

#[cfg(all(test, windows, feature = "legacy-workshop"))]
mod tests {
    use super::*;

    #[test]
    fn support_id_reads_an_expired_session_without_changing_it() {
        let home = tempfile::tempdir().unwrap();
        assert!(matches!(stored_support_id(home.path()), Ok(None)));
        let path = session_path(home.path()).unwrap_or_else(|_| panic!("invalid test home"));
        let account_id = "support-id-fixture";
        let payload = Payload {
            schema_version: 1,
            service_origin: service_origin().unwrap_or_else(|_| panic!("invalid test origin")),
            account_id,
            expires_at: 1,
            bearer_token: "mgr_0123456789abcdef01234567_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
            enrollment_id: "eni_0123456789abcdef0123456789abcdef",
            enrollment_secret: "ens_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq",
        };
        let plaintext = serde_json::to_vec(&payload).unwrap();
        let encrypted = protect_current_user(&plaintext)
            .unwrap_or_else(|_| panic!("could not protect test session"));
        let wire = serde_json::to_vec(&Envelope {
            schema_version: 1,
            protection: "windows-dpapi-current-user",
            payload: &STANDARD.encode(&encrypted),
        })
        .unwrap();
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, &wire).unwrap();
        assert!(matches!(
            load_current_user(home.path(), 2),
            Ok(SessionState::Expired)
        ));
        assert!(matches!(stored_support_id(home.path()), Ok(Some(id)) if id == account_id));
        assert_eq!(fs::read(path).unwrap(), wire);
    }
}
