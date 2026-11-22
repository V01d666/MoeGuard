//! Normalize an explicitly confirmed draft into the existing portable role format.
#[cfg(any(test, all(windows, feature = "pet-win32")))]
use super::expected_files;
#[cfg(any(
    test,
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
use super::png_header;
use super::{verify_tree, Collected, Kind, INVALID, MANIFEST, MAX_WIRE};
use crate::role_catalog::revision::{is_hex, is_remote, EditService, Outcome, STORAGE};
use serde_json::Value;
use std::{
    fs::{self, File},
    io::{self, BufReader, Read, Write},
    path::{Path, PathBuf},
};
use zip::{write::SimpleFileOptions, CompressionMethod, ZipArchive, ZipWriter};

const MAX_ARCHIVE: u64 = 64 * 1024 * 1024;
const TOO_LARGE: &str = "revision_review_too_large";
const MISMATCH: &str = "revision_review_mismatch";
const CONFLICT: &str = "revision_review_conflict";
const INSTALL_CONFLICT: &str = "revision_install_conflict";
const RECEIPT_LIMIT: u64 = 8192;

#[cfg(any(test, all(windows, feature = "pet-win32")))]
#[path = "portable.rs"]
pub(crate) mod portable;

#[cfg(test)]
#[test]
fn settings_role_transfer_round_trips_the_existing_site_zip() {
    use std::io::Cursor;
    let home = tempfile::tempdir().unwrap();
    let source = include_bytes!("../../../test/fixtures/showcase-role.zip");
    let imported = portable::install_zip(home.path(), Cursor::new(source)).unwrap();
    assert_eq!(imported.name, "Arona");
    assert_eq!(portable::list(home.path()).unwrap().len(), 1);
    assert!(!home.path().join("active-pet.json").exists());
    let bytes = portable::load(home.path(), &imported.key).unwrap();
    let other = tempfile::tempdir().unwrap();
    assert_eq!(
        portable::install_zip(other.path(), Cursor::new(bytes)).unwrap(),
        imported
    );
    assert!(portable::install_zip(other.path(), Cursor::new(b"invalid archive")).is_err());
    assert_eq!(portable::list(other.path()).unwrap().len(), 1);
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct InstallReceipt {
    schema: u8,
    result: Collected,
    spec: Value,
    #[allow(dead_code)] // displayed by the native playback library
    name: String,
}

#[derive(serde::Serialize)]
pub(in crate::role_catalog) struct Installation {
    #[serde(flatten)]
    confirmed: Collected,
    role_id: String,
    package_version: u64,
}

#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(feature = "pet-gtk-probe", target_os = "linux")
))]
#[path = "pet_selection.rs"]
mod pet_selection;
#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(test, target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) use pet_selection::restore_pet;
#[cfg(all(windows, feature = "pet-win32"))]
pub(crate) use pet_selection::{
    active_role_archive, clear_removed_pet, prepare_imported_pet, ready_roles,
    restore_playback_key, workshop_key,
};
#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(feature = "pet-gtk-probe", target_os = "linux")
))]
pub(crate) use pet_selection::{prepare_catalog_pet, PreparedPet};

fn playback_spec(spec: &Value) -> Value {
    serde_json::json!({
        "profile_id": spec["profile_id"], "identity_sha256": spec["identity_sha256"],
        "appearance_revision": spec["appearance_revision"],
        "package_version": spec["package_version"], "actions": spec["actions"],
    })
}

fn receipt_directory(directory: &Path) -> PathBuf {
    directory.join("installed-receipts")
}

fn remember_install(directory: &Path, result: &Collected, spec: &Value) -> Outcome<()> {
    let receipts = receipt_directory(directory);
    ensure_directory(&receipts)?;
    let bytes = serde_json::to_vec(&serde_json::json!({
        "schema": 1, "result": result, "spec": playback_spec(spec),
        "name": spec["display_name"].as_str().filter(|name| !name.trim().is_empty())
            .unwrap_or(version_key(spec)?.0).chars().take(80).collect::<String>(),
    }))
    .map_err(|_| STORAGE)?;
    if !is_remote(&result.remote_task_id) || bytes.len() as u64 > RECEIPT_LIMIT {
        return Err(STORAGE);
    }
    let mut file = tempfile::NamedTempFile::new_in(&receipts).map_err(|_| STORAGE)?;
    file.write_all(&bytes)
        .and_then(|_| file.as_file().sync_all())
        .map_err(|_| STORAGE)?;
    let path = receipts.join(format!("{}.json", result.remote_task_id));
    match file.persist_noclobber(&path) {
        Ok(_) => (),
        Err(error) if error.error.kind() == io::ErrorKind::AlreadyExists => {
            if !same_file(error.file.path(), &path)? {
                return Err(INSTALL_CONFLICT);
            }
        }
        Err(_) => return Err(STORAGE),
    }
    #[cfg(unix)]
    for dir in [&receipts, directory] {
        File::open(dir)
            .and_then(|file| file.sync_all())
            .map_err(|_| STORAGE)?;
    }
    Ok(())
}

fn read_receipt(path: &Path, remote: &str) -> Outcome<InstallReceipt> {
    require_cached(path, |meta| {
        meta.is_file() && (1..=RECEIPT_LIMIT).contains(&meta.len())
    })?;
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(RECEIPT_LIMIT + 1).read_to_end(&mut bytes))
        .map_err(|_| STORAGE)?;
    if bytes.len() as u64 > RECEIPT_LIMIT {
        return Err(STORAGE);
    }
    let entry: InstallReceipt = serde_json::from_slice(&bytes).map_err(|_| STORAGE)?;
    if entry.schema != 1
        || entry.result.remote_task_id != remote
        || !is_hex(&entry.result.result_sha256, 64)
    {
        return Err(MISMATCH);
    }
    version_key(&entry.spec)?;
    Ok(entry)
}

pub(in crate::role_catalog) fn custom_source(
    directory: &Path,
    id: &str,
    remote: &str,
) -> Outcome<(Value, Vec<u8>)> {
    if !is_remote(remote) {
        return Err(MISMATCH);
    }
    let receipts = receipt_directory(directory);
    require_cached(&receipts, |meta| meta.is_dir())?;
    let entry = read_receipt(&receipts.join(format!("{remote}.json")), remote)?;
    let (role, version) = version_key(&entry.spec)?;
    if role != id
        || directory
            .join("roles")
            .join(role)
            .join((version + 1).to_string())
            .exists()
    {
        return Err(MISMATCH);
    }
    verified_installation(directory, &entry.result, &entry.spec)?;
    let source = fs::read(reviewed_path(directory, remote)).map_err(|_| STORAGE)?;
    Ok((entry.spec, source))
}

#[cfg(any(
    test,
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) fn installed_pets(directory: &Path) -> Outcome<Value> {
    let receipts = receipt_directory(directory);
    match fs::symlink_metadata(&receipts) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(serde_json::json!([])),
        Ok(meta) if meta.is_dir() => (),
        _ => return Err(STORAGE),
    }
    let mut roles = Vec::new();
    for (index, entry) in fs::read_dir(receipts).map_err(|_| STORAGE)?.enumerate() {
        if index >= 2048 {
            return Err(STORAGE);
        }
        let entry = entry.map_err(|_| STORAGE)?;
        let name = entry.file_name();
        let Some(remote) = name
            .to_str()
            .and_then(|name| name.strip_suffix(".json"))
            .filter(|name| is_remote(name))
        else {
            continue;
        };
        let Ok(receipt) = read_receipt(&entry.path(), remote) else {
            continue;
        };
        let (role_id, package_version) = version_key(&receipt.spec)?;
        let target = directory
            .join("roles")
            .join(role_id)
            .join(package_version.to_string());
        let Ok(actions) = installed_actions(&target, role_id, package_version) else {
            continue;
        };
        roles.push((
            Installation {
                confirmed: receipt.result,
                role_id: role_id.to_owned(),
                package_version,
            },
            receipt.name,
            actions,
        ));
    }
    roles.sort_by(|(a, _, _), (b, _, _)| {
        a.role_id
            .cmp(&b.role_id)
            .then(b.package_version.cmp(&a.package_version))
            .then(a.confirmed.remote_task_id.cmp(&b.confirmed.remote_task_id))
    });
    roles.dedup_by(|(a, _, _), (b, _, _)| a.role_id == b.role_id);
    let roles: Outcome<Vec<_>> = roles
        .into_iter()
        .map(|(entry, name, actions)| {
            let cover = first_idle_cover(directory, &entry.role_id).ok();
            let mut row = serde_json::to_value(entry).map_err(|_| STORAGE)?;
            row["name"] = Value::String(name);
            row["actions"] = serde_json::to_value(actions).map_err(|_| STORAGE)?;
            if let Some(cover) = cover {
                row["cover"] = Value::String(cover);
            }
            Ok(row)
        })
        .collect();
    Ok(Value::Array(roles?))
}

#[cfg(any(
    test,
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
fn installed_manifest(target: &Path, role: &str, version: u64) -> Outcome<Value> {
    require_cached(target, |meta| meta.is_dir())?;
    let path = target.join("role.json");
    const LIMIT: u64 = 512 * 1024;
    require_cached(&path, |meta| meta.is_file() && meta.len() <= LIMIT)?;
    let manifest: Value =
        serde_json::from_reader(File::open(path).map_err(|_| STORAGE)?.take(LIMIT))
            .map_err(|_| MISMATCH)?;
    if manifest["role_id"] != role || manifest["package_version"] != version {
        return Err(MISMATCH);
    }
    Ok(manifest)
}

#[cfg(any(
    test,
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
fn first_idle_cover(directory: &Path, role: &str) -> Outcome<String> {
    use base64::{engine::general_purpose::STANDARD, Engine};
    // Custom roles start at version 1. Later edits never change this cover.
    // Catalog roles already have their original idle frame in the catalog inventory.
    let first = directory.join("roles").join(role).join("1");
    let manifest = installed_manifest(&first, role, 1)?;
    let frame = &manifest["actions"]["idle"]["frames"][0];
    const IDLE: &str = "actions/idle/0001.png";
    const LIMIT: u64 = 4 * 1024 * 1024;
    if frame["path"] != IDLE {
        return Err(MISMATCH);
    }
    let path = first.join(IDLE);
    require_cached(&path, |meta| {
        meta.is_file() && (1..=LIMIT).contains(&meta.len())
    })?;
    let mut bytes = Vec::new();
    File::open(path)
        .map_err(|_| STORAGE)?
        .take(LIMIT + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| STORAGE)?;
    if bytes.len() as u64 > LIMIT
        || !png_header(&bytes)
        || frame["sha256"] != crate::role_catalog::digest_hex(&bytes)
    {
        return Err(MISMATCH);
    }
    Ok(format!("data:image/png;base64,{}", STANDARD.encode(bytes)))
}

#[cfg(any(
    test,
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
fn installed_actions(target: &Path, role: &str, version: u64) -> Outcome<Vec<&'static str>> {
    let manifest = installed_manifest(target, role, version)?;
    let actions = manifest["actions"].as_object().ok_or(MISMATCH)?;
    Ok(crate::role_catalog::revision::ACTIONS
        .iter()
        .copied()
        .filter(|action| actions.contains_key(*action))
        .collect())
}

#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) fn installed_pet(
    directory: PathBuf,
    remote: &str,
    digest: &str,
) -> Outcome<PreparedPet> {
    let receipt = installed_receipt(&directory, remote, digest)?;
    PreparedPet::verify(directory, receipt.result, receipt.spec)
}

fn installed_receipt(directory: &Path, remote: &str, digest: &str) -> Outcome<InstallReceipt> {
    if !is_remote(remote) || !is_hex(digest, 64) {
        return Err(MISMATCH);
    }
    let receipts = receipt_directory(directory);
    require_cached(&receipts, |meta| meta.is_dir())?;
    let receipt = read_receipt(&receipts.join(format!("{remote}.json")), remote)?;
    if receipt.result.result_sha256 != digest {
        return Err(MISMATCH);
    }
    Ok(receipt)
}

pub(in crate::role_catalog) fn check_install_receipt(
    directory: &Path,
    remote: &str,
    digest: &str,
) -> Outcome<()> {
    installed_receipt(directory, remote, digest).map(|_| ())
}

#[cfg(all(windows, feature = "pet-win32"))]
pub(crate) fn installed_action_preview(
    directory: &Path,
    remote: &str,
    digest: &str,
    role_id: &str,
    action: &str,
    rate: Option<u8>,
) -> Outcome<Vec<u8>> {
    let receipt = installed_receipt(directory, remote, digest)?;
    if version_key(&receipt.spec)?.0 != role_id {
        return Err(MISMATCH);
    }
    let verified = verified_installation(directory, &receipt.result, &receipt.spec)?;
    crate::pet_frames::action_preview(action, rate, |name, limit| {
        let file = fs::File::open(verified.path().join(name)).map_err(|_| INVALID)?;
        crate::pet_frames::read_bytes(file, limit)
    })
}

impl EditService {
    pub(in crate::role_catalog) async fn install_motion(
        &self,
        remote: &str,
        digest: &str,
    ) -> Outcome<()> {
        let (confirmed, spec) = self.review_target(remote, digest).await?;
        let received = self.collect().await?;
        if received.remote_task_id != remote || received.result_sha256 != digest {
            return Err(MISMATCH);
        }
        let directory = self.directory.clone();
        tauri::async_runtime::spawn_blocking(move || {
            publish(&directory, &confirmed, &spec)?;
            install_version(&directory, confirmed, &spec).map(|_| ())
        })
        .await
        .map_err(|_| STORAGE)?
    }

    pub(in crate::role_catalog) async fn confirm(
        &self,
        remote: &str,
        digest: &str,
    ) -> Outcome<Collected> {
        let (confirmed, spec) = self.review_target(remote, digest).await?;
        let directory = self.directory.clone();
        tauri::async_runtime::spawn_blocking(move || {
            publish(&directory, &confirmed, &spec)?;
            Ok(confirmed)
        })
        .await
        .map_err(|_| STORAGE)?
    }

    pub(in crate::role_catalog) async fn install(
        &self,
        remote: &str,
        digest: &str,
    ) -> Outcome<Installation> {
        let (confirmed, spec) = self.review_target(remote, digest).await?;
        let directory = self.directory.clone();
        tauri::async_runtime::spawn_blocking(move || install_version(&directory, confirmed, &spec))
            .await
            .map_err(|_| STORAGE)?
    }

    pub(in crate::role_catalog) async fn verify_finished_install(
        &self,
        remote: &str,
        digest: &str,
    ) -> Outcome<()> {
        let (confirmed, spec) = self.review_target(remote, digest).await?;
        let directory = self.directory.clone();
        tauri::async_runtime::spawn_blocking(move || {
            verified_installation(&directory, &confirmed, &spec)?;
            remember_install(&directory, &confirmed, &spec)
        })
        .await
        .map_err(|_| STORAGE)?
    }

    #[cfg(any(
        all(windows, feature = "pet-win32"),
        all(feature = "pet-gtk-probe", target_os = "linux")
    ))]
    pub(in crate::role_catalog) async fn prepare_pet(
        &self,
        remote: &str,
        digest: &str,
    ) -> Outcome<PreparedPet> {
        let (confirmed, spec) = self.review_target(remote, digest).await?;
        let directory = self.directory.clone();
        tauri::async_runtime::spawn_blocking(move || {
            PreparedPet::verify(directory, confirmed, spec)
        })
        .await
        .map_err(|_| STORAGE)?
    }

    pub(in crate::role_catalog) fn has_reviewed_archive(&self, remote: &str) -> bool {
        // A UI hint only. Installation re-verifies all bytes and does not trust this flag.
        is_remote(remote)
            && fs::symlink_metadata(reviewed_path(&self.directory, remote))
                .is_ok_and(|meta| meta.is_file() && (1..=MAX_ARCHIVE).contains(&meta.len()))
    }

    pub(in crate::role_catalog) fn has_installed_version(
        &self,
        remote: &str,
        spec: &Value,
    ) -> bool {
        // Only a display hint; activation still verifies the complete result and installed tree.
        fn matching(directory: &Path, remote: &str, spec: &Value) -> Option<bool> {
            const LIMIT: u64 = 512 * 1024;
            fn manifest_bytes(reader: impl Read) -> Option<Vec<u8>> {
                let mut bytes = Vec::new();
                reader.take(LIMIT + 1).read_to_end(&mut bytes).ok()?;
                (1..=LIMIT).contains(&(bytes.len() as u64)).then_some(bytes)
            }
            let (role_id, version) = version_key(spec).ok()?;
            let library = directory.join("roles");
            let versions = library.join(role_id);
            let target = versions.join(version.to_string());
            let reviewed = reviewed_path(directory, remote);
            for dir in [library.as_path(), &versions, &target, reviewed.parent()?] {
                if !fs::symlink_metadata(dir).ok()?.is_dir() {
                    return None;
                }
            }
            let path = target.join("role.json");
            if !fs::symlink_metadata(&path).ok()?.is_file() {
                return None;
            }
            let actual = manifest_bytes(File::open(path).ok()?)?;
            let mut archive = ZipArchive::new(File::open(reviewed).ok()?).ok()?;
            let approved = manifest_bytes(archive.by_name("role.json").ok()?)?;
            Some(actual == approved)
        }
        matching(&self.directory, remote, spec).unwrap_or(false)
    }

    async fn review_target(&self, remote: &str, digest: &str) -> Outcome<(Collected, Value)> {
        let (_, frozen) = self.load()?.ok_or("revision_no_pending")?;
        if !is_remote(remote) || !is_hex(digest, 64) || frozen.remote.as_deref() != Some(remote) {
            return Err(MISMATCH);
        }
        let snapshot = self.query_bound(&frozen).await?;
        if snapshot.status != "succeeded" {
            return Err("revision_not_ready");
        }
        if snapshot.result_sha256 != digest {
            return Err(MISMATCH);
        }
        let confirmed = Collected {
            remote_task_id: snapshot.remote_task_id,
            result_sha256: snapshot.result_sha256,
        };
        let mut wire: Value = serde_json::from_str(&frozen.body).map_err(|_| STORAGE)?;
        let mut spec = wire["request"]["spec"].take();
        if spec["operation"] == "identity_candidates" {
            return Err(MISMATCH);
        }
        spec.as_object_mut().ok_or(STORAGE)?.insert(
            "display_name".into(),
            wire["request"]["profile"]["display_name"].take(),
        );
        Ok((confirmed, spec))
    }
}

fn approve_manifest(data: &[u8]) -> Outcome<Vec<u8>> {
    let mut manifest: Value = serde_json::from_slice(data).map_err(|_| INVALID)?;
    let actions = manifest["actions"].as_object().ok_or(INVALID)?;
    let quality = manifest["quality"]["actions"].as_object().ok_or(INVALID)?;
    if actions.keys().ne(quality.keys()) {
        return Err(INVALID);
    }
    for (action, review) in manifest["quality"]["actions"]
        .as_object_mut()
        .ok_or(INVALID)?
    {
        if !matches!(review["status"].as_str(), Some("pending" | "accepted")) {
            return Err(INVALID);
        }
        if matches!(action.as_str(), "peek_left" | "peek_right") {
            if !matches!(
                review["direction"]["status"].as_str(),
                Some("pending" | "accepted")
            ) {
                return Err(INVALID);
            }
            review["direction"]["status"] = Value::from("accepted");
        }
        review["status"] = Value::from("accepted");
    }
    manifest["quality"]["status"] = Value::from("accepted");
    manifest["rights"] = serde_json::json!({
        "input_rights_confirmed": true, "output_accepted": true, "scope": "private-use"
    });
    serde_json::to_vec(&manifest).map_err(|_| INVALID)
}

fn require_cached(path: &Path, predicate: impl FnOnce(&fs::Metadata) -> bool) -> Outcome<()> {
    match fs::symlink_metadata(path) {
        Ok(meta) if predicate(&meta) => Ok(()),
        Ok(_) => Err(INVALID),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Err("revision_review_not_cached")
        }
        Err(_) => Err(STORAGE),
    }
}

fn reviewed_path(directory: &Path, remote: &str) -> std::path::PathBuf {
    directory
        .join("reviewed")
        .join(format!("{remote}.moeguard-role"))
}

fn build_archive(
    directory: &Path,
    result: &Collected,
    spec: &Value,
    stage_in: &Path,
    mut visit: impl FnMut(&str, &[u8]) -> Outcome<()>,
) -> Outcome<tempfile::NamedTempFile> {
    let results = directory.join("results");
    require_cached(&results, |meta| meta.is_dir())?;
    let source = results.join(format!("{}.zip", result.remote_task_id));
    require_cached(&source, |meta| meta.is_file() && meta.len() <= MAX_WIRE)?;
    let source = File::open(source).map_err(|_| STORAGE)?;
    let mut staged = tempfile::NamedTempFile::new_in(stage_in).map_err(|_| STORAGE)?;
    let mut archive = ZipWriter::new(staged.as_file_mut());
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Stored);
    let mut payload_bytes = 0;
    verify_tree(source, spec, &result.result_sha256, |name, kind, bytes| {
        let normalized;
        let data = if name == MANIFEST {
            normalized = approve_manifest(bytes)?;
            normalized.as_slice()
        } else if matches!(kind, Kind::Png(_)) {
            bytes
        } else {
            // The old installer expects only the manifest and listed PNGs.
            return Ok(());
        };
        payload_bytes += data.len() as u64;
        if payload_bytes > MAX_ARCHIVE {
            return Err(TOO_LARGE);
        }
        let relative = name.strip_prefix("package/").ok_or(INVALID)?;
        archive.start_file(relative, options).map_err(|_| STORAGE)?;
        archive.write_all(data).map_err(|_| STORAGE)?;
        visit(relative, data)
    })?;
    archive.finish().map_err(|_| STORAGE)?;
    let length = staged.as_file().metadata().map_err(|_| STORAGE)?.len();
    if length > MAX_ARCHIVE {
        return Err(TOO_LARGE);
    }
    staged.as_file().sync_all().map_err(|_| STORAGE)?;
    Ok(staged)
}

fn ensure_directory(path: &Path) -> Outcome<()> {
    match fs::create_dir(path) {
        Ok(()) => Ok(()),
        Err(error)
            if error.kind() == io::ErrorKind::AlreadyExists
                && fs::symlink_metadata(path).is_ok_and(|meta| meta.is_dir()) =>
        {
            Ok(())
        }
        Err(_) => Err(STORAGE),
    }
}

fn publish(directory: &Path, result: &Collected, spec: &Value) -> Outcome<()> {
    let reviewed = directory.join("reviewed");
    ensure_directory(&reviewed)?;
    let staged = build_archive(directory, result, spec, &reviewed, |_, _| Ok(()))?;
    let target = reviewed_path(directory, &result.remote_task_id);
    match staged.persist_noclobber(&target) {
        Ok(_) => (),
        Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {
            if !same_file(error.file.path(), &target)? {
                return Err(CONFLICT);
            }
        }
        Err(_) => return Err(STORAGE),
    }
    #[cfg(unix)]
    for dir in [&reviewed, directory] {
        File::open(dir)
            .and_then(|file| file.sync_all())
            .map_err(|_| STORAGE)?;
    }
    Ok(())
}

fn stage_version(
    directory: &Path,
    confirmed: &Collected,
    spec: &Value,
    library: &Path,
    durable: bool,
) -> Outcome<tempfile::TempDir> {
    let reviewed = reviewed_path(directory, &confirmed.remote_task_id);
    for (path, is_dir) in [
        (reviewed.parent().ok_or(STORAGE)?, true),
        (reviewed.as_path(), false),
    ] {
        match fs::symlink_metadata(path) {
            Ok(meta)
                if is_dir && meta.is_dir()
                    || !is_dir && meta.is_file() && meta.len() <= MAX_ARCHIVE =>
            {
                ()
            }
            Ok(_) => return Err(CONFLICT),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return Err("revision_install_not_reviewed")
            }
            Err(_) => return Err(STORAGE),
        }
    }
    let staging = tempfile::Builder::new()
        .prefix(".install-")
        .tempdir_in(library)
        .map_err(|_| STORAGE)?;
    let expected = build_archive(directory, confirmed, spec, library, |name, data| {
        // Names were derived from the validated action enum and each frame's ordinal.
        let path = staging.path().join(name);
        fs::create_dir_all(path.parent().ok_or(STORAGE)?).map_err(|_| STORAGE)?;
        let mut file = File::create_new(path).map_err(|_| STORAGE)?;
        file.write_all(data).map_err(|_| STORAGE)?;
        if durable {
            file.sync_all().map_err(|_| STORAGE)?;
        }
        Ok(())
    })?;
    if !same_file(expected.path(), &reviewed)? {
        return Err(CONFLICT);
    }
    Ok(staging)
}

fn verified_installation(
    directory: &Path,
    result: &Collected,
    spec: &Value,
) -> Outcome<tempfile::TempDir> {
    let (role_id, version) = version_key(spec)?;
    let library = directory.join("roles");
    let versions = library.join(role_id);
    let target = versions.join(version.to_string());
    for path in [&library, &versions, &target] {
        match fs::symlink_metadata(path) {
            Ok(meta) if meta.is_dir() => (),
            Ok(_) => return Err(INSTALL_CONFLICT),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return Err("revision_pet_not_installed")
            }
            Err(_) => return Err(STORAGE),
        }
    }
    let staging = stage_version(directory, result, spec, &library, false)?;
    if !same_tree(staging.path(), &target)? {
        return Err(INSTALL_CONFLICT);
    }
    Ok(staging)
}

fn version_key(spec: &Value) -> Outcome<(&str, u64)> {
    let role_id = spec["profile_id"]
        .as_str()
        .filter(|id| {
            let ascii_id = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit();
            (3..=48).contains(&id.len())
                && id.bytes().next().is_some_and(ascii_id)
                && id.bytes().all(|b| ascii_id(b) || b == b'-')
        })
        .ok_or(INVALID)?;
    let version = spec["package_version"]
        .as_u64()
        .filter(|v| (1..=i32::MAX as u64).contains(v))
        .ok_or(INVALID)?;
    Ok((role_id, version))
}

fn install_version(directory: &Path, confirmed: Collected, spec: &Value) -> Outcome<Installation> {
    let (role_id, package_version) = version_key(spec)?;
    let library = directory.join("roles");
    ensure_directory(&library)?;
    let staging = stage_version(directory, &confirmed, spec, &library, true)?;
    // Serializes publishers to this isolated library across native processes.
    let lock_path = library.join(".install.lock");
    match fs::symlink_metadata(&lock_path) {
        Ok(meta) if meta.is_file() => (),
        Err(error) if error.kind() == io::ErrorKind::NotFound => (),
        _ => return Err(STORAGE),
    }
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(lock_path)
        .map_err(|_| STORAGE)?;
    lock.lock().map_err(|_| STORAGE)?;
    let versions = library.join(role_id);
    ensure_directory(&versions)?;
    let target = versions.join(package_version.to_string());
    match fs::symlink_metadata(&target) {
        Ok(_) if same_tree(staging.path(), &target)? => (),
        Ok(_) => return Err(INSTALL_CONFLICT),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            #[cfg(unix)]
            sync_staged_directories(staging.path())?;
            fs::rename(staging.path(), &target).map_err(|_| STORAGE)?;
        }
        Err(_) => return Err(STORAGE),
    }
    #[cfg(unix)]
    for dir in [&target, &versions, &library, directory] {
        File::open(dir)
            .and_then(|file| file.sync_all())
            .map_err(|_| STORAGE)?;
    }
    remember_install(directory, &confirmed, spec)?;
    Ok(Installation {
        confirmed,
        role_id: role_id.to_owned(),
        package_version,
    })
}

#[cfg(unix)]
fn sync_staged_directories(path: &Path) -> Outcome<()> {
    for entry in fs::read_dir(path).map_err(|_| STORAGE)? {
        let entry = entry.map_err(|_| STORAGE)?;
        if entry.file_type().map_err(|_| STORAGE)?.is_dir() {
            sync_staged_directories(&entry.path())?;
        }
    }
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|_| STORAGE)
}

fn same_tree(expected: &Path, actual: &Path) -> Outcome<bool> {
    if !fs::symlink_metadata(actual).is_ok_and(|meta| meta.is_dir()) {
        return Ok(false);
    }
    let mut size = 0;
    for entry in fs::read_dir(expected).map_err(|_| STORAGE)? {
        let entry = entry.map_err(|_| STORAGE)?;
        size += 1;
        let current = actual.join(entry.file_name());
        let matched = if entry.file_type().map_err(|_| STORAGE)?.is_dir() {
            same_tree(&entry.path(), &current)?
        } else {
            same_file(&entry.path(), &current)?
        };
        if !matched {
            return Ok(false);
        }
    }
    Ok(fs::read_dir(actual)
        .map_err(|_| STORAGE)?
        .take(size + 1)
        .count()
        == size)
}

fn same_file(expected: &Path, actual: &Path) -> Outcome<bool> {
    let length = fs::metadata(expected).map_err(|_| STORAGE)?.len();
    if !fs::symlink_metadata(actual).is_ok_and(|meta| meta.is_file() && meta.len() == length) {
        return Ok(false);
    }
    let left = File::open(expected).map_err(|_| STORAGE)?;
    let right = File::open(actual).map_err(|_| STORAGE)?;
    same_bytes(BufReader::new(left), BufReader::new(right), length)
}

fn same_bytes(mut a: impl Read, mut b: impl Read, mut length: u64) -> Outcome<bool> {
    let (mut left, mut right) = ([0; 8192], [0; 8192]);
    while length > 0 {
        let count = length.min(left.len() as u64) as usize;
        a.read_exact(&mut left[..count]).map_err(|_| STORAGE)?;
        b.read_exact(&mut right[..count]).map_err(|_| STORAGE)?;
        if left[..count] != right[..count] {
            return Ok(false);
        }
        length -= count as u64;
    }
    Ok(b.read(&mut right[..1]).map_err(|_| STORAGE)? == 0)
}
