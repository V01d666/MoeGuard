use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::{redirect::Policy, Client, StatusCode};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{Cursor, Read, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
    time::Duration,
};
use tokio_util::sync::CancellationToken;

#[cfg(all(not(debug_assertions), feature = "catalog-loopback"))]
compile_error!("catalog-loopback is for development builds only");
#[cfg(feature = "catalog-loopback")]
const BASE: &str = "http://127.0.0.1:18463";
#[cfg(not(feature = "catalog-loopback"))]
const BASE: &str = "https://moeguard.com/assets/desktop-roles/v1";
const MANIFEST: &str = include_str!("../../../role-catalog-v1.json");
const WHALE: &[u8] = include_bytes!("../../../assets/whale.zip");
const MAX_PORTRAIT: u64 = 2 * 1024 * 1024;
const IO: &str = "catalog_storage";
const BAD: &str = "catalog_integrity";
const NET: &str = "catalog_network";
const STOP: &str = "catalog_cancelled";
type Outcome<T> = Result<T, &'static str>;

#[derive(Deserialize)]
struct Entry {
    id: String,
    #[cfg(any(
        all(windows, feature = "pet-win32"),
        feature = "revision-loopback",
        test
    ))]
    role_id: String,
    name: String,
    actions: Vec<String>,
    bytes: usize,
    sha256: String,
    bundled: bool,
}

fn entries() -> &'static [Entry] {
    static ENTRIES: OnceLock<Vec<Entry>> = OnceLock::new();
    ENTRIES.get_or_init(|| serde_json::from_str(MANIFEST).expect("invalid compiled catalog"))
}

#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(test, target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) fn bundled_pet() -> Outcome<crate::pet_frames::Frames> {
    let entry = entries()
        .iter()
        .find(|entry| entry.id == "whale" && entry.bundled)
        .ok_or(BAD)?;
    decode_pet(entry, WHALE)
}

#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
fn decode_pet(entry: &Entry, bytes: &[u8]) -> Outcome<crate::pet_frames::Frames> {
    use crate::pet_frames::{read_bytes, Frames};
    entry.verify(bytes)?;
    let mut outer = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|_| BAD)?;
    let source = read_bytes(
        outer.by_name("source.moeguard-role").map_err(|_| BAD)?,
        25 * 1024 * 1024,
    )?;
    let mut role = zip::ZipArchive::new(Cursor::new(source)).map_err(|_| BAD)?;
    Frames::read_members("click_reaction", |name, limit| {
        let file = role.by_name(name).map_err(|_| BAD)?;
        if file.is_dir() || file.size() > limit {
            return Err(BAD);
        }
        read_bytes(file, limit)
    })
}

#[derive(Debug, Serialize)]
pub struct Role {
    id: String,
    name: String,
    portrait: Portrait,
    actions: Vec<String>,
}

#[derive(Debug, Serialize)]
struct Portrait {
    id: String,
    src: String,
}

impl Entry {
    fn verify(&self, bytes: &[u8]) -> Outcome<()> {
        if bytes.len() != self.bytes || digest_hex(bytes) != self.sha256 {
            return Err(BAD);
        }
        Ok(())
    }

    fn view(&self, bytes: &[u8]) -> Outcome<Role> {
        self.verify(bytes)?;
        // Build-time packaging validates the full source archive and binds the
        // profile to the unchanged idle frame. Do not extract any paths here.
        let mut zip = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|_| BAD)?;
        let mut file = zip.by_name("identity.png").map_err(|_| BAD)?;
        let mut png = Vec::new();
        (&mut file)
            .take(MAX_PORTRAIT + 1)
            .read_to_end(&mut png)
            .map_err(|_| BAD)?;
        if png.len() as u64 > MAX_PORTRAIT || !png.starts_with(b"\x89PNG\r\n\x1a\n") {
            return Err(BAD);
        }
        Ok(Role {
            id: self.id.clone(),
            name: self.name.clone(),
            actions: self.actions.clone(),
            portrait: Portrait {
                id: self.id.clone(),
                src: format!("data:image/png;base64,{}", STANDARD.encode(png)),
            },
        })
    }
}

fn digest_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[derive(Default)]
pub struct Downloads(Arc<Mutex<Option<Pending>>>);

struct Pending {
    id: String,
    stop: CancellationToken,
    committed: bool,
}

struct Lease {
    active: Arc<Mutex<Option<Pending>>>,
    stop: CancellationToken,
}

impl Downloads {
    fn begin(&self, id: &str) -> Outcome<Lease> {
        let mut active = self.0.lock().map_err(|_| IO)?;
        if active.is_some() {
            return Err("catalog_busy");
        }
        let stop = CancellationToken::new();
        *active = Some(Pending {
            id: id.into(),
            stop: stop.clone(),
            committed: false,
        });
        Ok(Lease {
            active: self.0.clone(),
            stop,
        })
    }

    pub fn cancel(&self, id: &str) -> bool {
        let Ok(active) = self.0.lock() else {
            return false;
        };
        let Some(job) = active.as_ref().filter(|job| job.id == id && !job.committed) else {
            return false;
        };
        job.stop.cancel();
        true
    }
}

impl Lease {
    fn finish(&self, action: impl FnOnce() -> Outcome<()>) -> Outcome<()> {
        let mut active = self.active.lock().map_err(|_| IO)?;
        if self.stop.is_cancelled() {
            return Err(STOP);
        }
        action()?;
        active.as_mut().expect("active catalog lease").committed = true;
        Ok(())
    }
}

impl Drop for Lease {
    fn drop(&mut self) {
        if let Ok(mut active) = self.active.lock() {
            *active = None;
        }
    }
}

pub struct Catalog {
    directory: PathBuf,
    base: String,
    downloads: Downloads,
}

impl Catalog {
    #[cfg(all(windows, feature = "pet-win32", feature = "legacy-workshop"))]
    pub(crate) fn action_preview(
        &self,
        id: &str,
        action: &str,
        rate: Option<u8>,
    ) -> Outcome<Vec<u8>> {
        let mut zip =
            zip::ZipArchive::new(Cursor::new(self.source_for_transfer(id)?)).map_err(|_| BAD)?;
        crate::pet_frames::action_preview(action, rate, |name, limit| {
            let file = zip.by_name(name).map_err(|_| BAD)?;
            if file.is_dir() || file.size() > limit {
                return Err(BAD);
            }
            crate::pet_frames::read_bytes(file, limit)
        })
    }

    #[cfg(all(windows, feature = "pet-win32", feature = "legacy-workshop"))]
    fn source_for_transfer(&self, id: &str) -> Outcome<Vec<u8>> {
        let entry = entries()
            .iter()
            .find(|entry| entry.id == id)
            .ok_or("catalog_unknown")?;
        let cached;
        let bytes = if entry.bundled {
            WHALE
        } else {
            cached = self.cached_bytes(entry)?.ok_or("catalog_not_installed")?;
            &cached
        };
        entry.verify(bytes)?;
        let mut zip = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|_| BAD)?;
        let source = zip.by_name("source.moeguard-role").map_err(|_| BAD)?;
        crate::pet_frames::read_bytes(source, 25 * 1024 * 1024)
    }
    #[cfg(any(
        all(
            windows,
            feature = "pet-win32",
            any(feature = "legacy-workshop", feature = "revision-loopback")
        ),
        all(target_os = "linux", feature = "pet-gtk-probe")
    ))]
    fn pet(&self, id: &str) -> Outcome<crate::pet_frames::Frames> {
        let entry = entries()
            .iter()
            .find(|entry| entry.id == id)
            .ok_or("catalog_unknown")?;
        if entry.bundled {
            return decode_pet(entry, WHALE);
        }
        let bytes = self.cached_bytes(entry)?.ok_or("catalog_not_installed")?;
        decode_pet(entry, &bytes)
    }
    pub fn production(root: &Path) -> Self {
        Self::new(root, BASE.into())
    }

    fn new(root: &Path, base: String) -> Self {
        Self {
            directory: root.join("catalog-v1"),
            base,
            downloads: Downloads::default(),
        }
    }

    fn file(&self, entry: &Entry) -> PathBuf {
        self.directory.join(format!("{}.zip", entry.id))
    }

    fn cached(&self, entry: &Entry) -> Outcome<Option<Role>> {
        Ok(self
            .cached_bytes(entry)?
            .and_then(|bytes| entry.view(&bytes).ok()))
    }

    fn cached_bytes(&self, entry: &Entry) -> Outcome<Option<Vec<u8>>> {
        let path = self.file(entry);
        let metadata = match fs::symlink_metadata(&path) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(IO),
        };
        if !metadata.is_file() || metadata.len() != entry.bytes as u64 {
            return Ok(None);
        }
        let mut bytes = Vec::new();
        File::open(path)
            .map_err(|_| IO)?
            .take(entry.bytes as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| IO)?;
        Ok(Some(bytes))
    }

    pub fn inventory(&self) -> Outcome<Vec<Role>> {
        let mut roles = Vec::new();
        for entry in entries() {
            if entry.bundled {
                roles.push(entry.view(WHALE)?);
            } else if let Some(role) = self.cached(entry)? {
                roles.push(role);
            }
        }
        Ok(roles)
    }

    pub fn cancel(&self, id: &str) -> bool {
        self.downloads.cancel(id)
    }

    pub async fn download(self: &Arc<Self>, id: &str, progress: impl FnMut(u8)) -> Outcome<Role> {
        let entry = entries()
            .iter()
            .find(|e| e.id == id)
            .ok_or("catalog_unknown")?;
        if entry.bundled {
            return entry.view(WHALE);
        }
        let lease = self.downloads.begin(id)?;
        let this = self.clone();
        let cached = tauri::async_runtime::spawn_blocking(move || this.cached(entry))
            .await
            .map_err(|_| IO)??;
        if let Some(role) = cached {
            lease.finish(|| Ok(()))?;
            return Ok(role);
        }
        let bytes = tokio::select! {
            biased;
            _ = lease.stop.cancelled() => return Err(STOP),
            result = self.fetch(entry, progress) => result?,
        };
        let this = self.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let role = entry.view(&bytes)?;
            fs::create_dir_all(&this.directory).map_err(|_| IO)?;
            let mut temp = tempfile::NamedTempFile::new_in(&this.directory).map_err(|_| IO)?;
            temp.write_all(&bytes).map_err(|_| IO)?;
            temp.as_file().sync_all().map_err(|_| IO)?;
            lease.finish(|| temp.persist(this.file(entry)).map(|_| ()).map_err(|_| IO))?;
            Ok(role)
        })
        .await
        .map_err(|_| IO)?
    }

    async fn fetch(&self, entry: &Entry, mut progress: impl FnMut(u8)) -> Outcome<Vec<u8>> {
        let client = Client::builder()
            .redirect(Policy::none())
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|_| NET)?;
        let mut response = client
            .get(format!("{}/{}.zip", self.base, entry.id))
            .send()
            .await
            .map_err(|_| NET)?;
        if response.status() != StatusCode::OK {
            return Err(NET);
        }
        if response
            .content_length()
            .is_some_and(|n| n != entry.bytes as u64)
        {
            return Err(BAD);
        }
        let mut bytes = Vec::with_capacity(entry.bytes);
        let mut last = 0;
        while let Some(chunk) = response.chunk().await.map_err(|_| NET)? {
            if chunk.len() > entry.bytes - bytes.len() {
                return Err(BAD);
            }
            bytes.extend_from_slice(&chunk);
            let current = (bytes.len() * 99 / entry.bytes) as u8;
            if current > last {
                progress(current);
                last = current;
            }
        }
        Ok(bytes)
    }
}

#[cfg(test)]
mod tests;

#[cfg(all(feature = "revision-loopback", not(debug_assertions)))]
compile_error!("revision-loopback is for isolated development tests only");

#[cfg(all(
    feature = "legacy-workshop",
    any(not(windows), feature = "revision-loopback")
))]
compile_error!("legacy-workshop requires Windows and is incompatible with revision-loopback");

#[cfg(any(
    feature = "revision-loopback",
    all(windows, feature = "legacy-workshop")
))]
pub(crate) mod revision;

#[cfg(all(test, feature = "revision-loopback"))]
mod revision_tests;
