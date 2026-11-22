//! Read-only result transfer for the isolated revision bridge. Never extract an HTTP ZIP.
use super::{
    is_hex, EditService, Frozen, Outcome, Snapshot, ACTIONS, INVALID as INVALID_SELECTION, NETWORK,
    REJECTED, STORAGE,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::{header::CONTENT_TYPE, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Seek, Write},
    path::Path,
};
use zip::ZipArchive;

#[path = "revision_review.rs"]
mod review;
#[cfg(any(
    test,
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) use review::installed_pets;
#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(test, target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) use review::restore_pet;
#[cfg(all(windows, feature = "pet-win32"))]
pub(crate) use review::{
    active_role_archive, clear_removed_pet, portable, prepare_imported_pet, ready_roles,
    restore_playback_key, workshop_key,
};
pub(super) use review::{check_install_receipt, custom_source};
#[cfg(all(windows, feature = "pet-win32"))]
pub(crate) use review::{installed_action_preview, PreparedPet};
#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) use review::{installed_pet, prepare_catalog_pet};

const INVALID: &str = "result_integrity";
const MEDIA_TYPE: &str = "application/vnd.moeguard.result+zip";
const MAX_WIRE: u64 = 128 * 1024 * 1024;
const MAX_EXPANDED: u64 = 512 * 1024 * 1024;
const META: &str = ".workbench-result.json";
const MANIFEST: &str = "package/role.json";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(in crate::role_catalog) struct Collected {
    remote_task_id: String,
    result_sha256: String,
}

impl EditService {
    pub(super) async fn preview(&self, action: &str) -> Outcome<Value> {
        if !ACTIONS.contains(&action) {
            return Err(INVALID_SELECTION);
        }
        let (_, frozen) = self.load()?.ok_or("revision_no_pending")?;
        let wire: Value = serde_json::from_str(&frozen.body).map_err(|_| STORAGE)?;
        let spec = wire["request"]["spec"].clone();
        if !spec["actions"]
            .as_array()
            .is_some_and(|items| items.iter().any(|item| item == action))
        {
            return Err(INVALID_SELECTION);
        }
        let result = self.collect().await?;
        if frozen.remote.as_deref() != Some(result.remote_task_id.as_str()) {
            return Err(INVALID);
        }
        let target = self
            .directory
            .join("results")
            .join(format!("{}.zip", result.remote_task_id));
        let action = action.to_owned();
        tauri::async_runtime::spawn_blocking(move || {
            let wanted = format!("package/previews/{action}.gif");
            let mut src = None;
            let archive = File::open(target).map_err(|_| STORAGE)?;
            verify_tree(archive, &spec, &result.result_sha256, |name, _, bytes| {
                if name == wanted {
                    if bytes.len() > 8 * 1024 * 1024 {
                        return Err("revision_preview_too_large");
                    }
                    src = Some(format!("data:image/gif;base64,{}", STANDARD.encode(bytes)));
                }
                Ok(())
            })?;
            Ok(serde_json::json!({"remote_task_id":result.remote_task_id,
                "result_sha256":result.result_sha256,"action":action,"src":src.ok_or(INVALID)?}))
        })
        .await
        .map_err(|_| STORAGE)?
    }

    pub(in crate::role_catalog) async fn collect(&self) -> Outcome<Collected> {
        let (_, frozen) = self.load()?.ok_or("revision_no_pending")?;
        // Collection has no path that can post or rebind a task.
        let snapshot = self.query_bound(&frozen).await?;
        self.collect_bound(&frozen, snapshot).await
    }

    async fn portrait_target(
        &self,
        remote: &str,
        digest: &str,
        allow_finished: bool,
    ) -> Outcome<(u32, Frozen, Value, Snapshot)> {
        let entry = if allow_finished {
            self.load_checkpoint()?
        } else {
            self.load()?
        };
        let (revision, frozen) = entry.ok_or("revision_no_pending")?;
        if frozen.remote.as_deref() != Some(remote) {
            return Err(INVALID_SELECTION);
        }
        let wire: Value = serde_json::from_str(&frozen.body).map_err(|_| STORAGE)?;
        portrait_count(&wire["request"])?;
        let snapshot = self.query_bound(&frozen).await?;
        if snapshot.status != "succeeded" {
            return Err("revision_not_ready");
        }
        if !is_hex(digest, 64) || snapshot.result_sha256 != digest {
            return Err("revision_review_mismatch");
        }
        Ok((revision, frozen, wire, snapshot))
    }

    pub(super) async fn finish_portraits(&self, remote: &str, digest: &str) -> Outcome<()> {
        let (revision, mut frozen, wire, _) = self.portrait_target(remote, digest, true).await?;
        // A lost IPC acknowledgement must not trap the already-saved portrait in the UI.
        if frozen.dismissed {
            return Ok(());
        }
        let dir = self.directory.join("results");
        match fs::symlink_metadata(&dir) {
            Ok(meta) if meta.is_dir() => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err("revision_review_not_cached")
            }
            _ => return Err(STORAGE),
        }
        if !verify_existing_async(&dir.join(format!("{remote}.zip")), digest, &wire["request"])
            .await?
        {
            return Err("revision_review_not_cached");
        }
        frozen.dismissed = true;
        self.save(revision, &frozen)
    }

    pub(super) async fn portrait_gallery(&self, remote: &str, digest: &str) -> Outcome<Value> {
        let (_, frozen, wire, snapshot) = self.portrait_target(remote, digest, false).await?;
        let count = portrait_count(&wire["request"])?;
        let profile = &wire["request"]["profile"];
        let name = profile["display_name"].as_str().ok_or(STORAGE)?.to_owned();
        let profile_id = profile["profile_id"].as_str().ok_or(STORAGE)?.to_owned();
        let result = self.collect_bound(&frozen, snapshot).await?;
        let path = self.directory.join("results").join(format!("{remote}.zip"));
        tauri::async_runtime::spawn_blocking(move || {
            let mut items = Vec::with_capacity(count);
            verify_portraits(File::open(path).map_err(|_| STORAGE)?, count, &result.result_sha256, |i, bytes| {
                items.push(serde_json::json!({"id":format!("{}-{}", result.remote_task_id, i),
                    "src":format!("data:image/png;base64,{}", STANDARD.encode(bytes))}));
            })?;
            Ok(serde_json::json!({"remote_task_id":result.remote_task_id,
                "result_sha256":result.result_sha256,"profile_id":profile_id,"name":name,"items":items}))
        }).await.map_err(|_| STORAGE)?
    }

    async fn collect_bound(&self, frozen: &Frozen, snapshot: Snapshot) -> Outcome<Collected> {
        if snapshot.status != "succeeded" {
            return Err("revision_not_ready");
        }
        if !is_hex(&snapshot.result_sha256, 64) {
            return Err(INVALID);
        }
        let dir = self.directory.join("results");
        fs::create_dir_all(&dir).map_err(|_| STORAGE)?;
        if !fs::symlink_metadata(&dir).map_err(|_| STORAGE)?.is_dir() {
            return Err(STORAGE);
        }
        let remote = snapshot.remote_task_id;
        let expected = snapshot.result_sha256;
        let target = dir.join(format!("{remote}.zip"));
        let wire: Value = serde_json::from_str(&frozen.body).map_err(|_| STORAGE)?;
        let request = wire["request"].clone();
        if !verify_existing_async(&target, &expected, &request).await? {
            let mut response = self
                .http
                .get(format!("{}/v1/tasks/{remote}/result", self.base))
                .bearer_auth(self.bearer.as_str())
                .send()
                .await
                .map_err(|_| NETWORK)?;
            self.auth_failure(response.status())?;
            if response.status() != StatusCode::OK {
                return Err(REJECTED);
            }
            if response
                .headers()
                .get(CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                != Some(MEDIA_TYPE)
                || response
                    .headers()
                    .get("x-moeguard-tree-sha256")
                    .and_then(|v| v.to_str().ok())
                    != Some(expected.as_str())
                || response
                    .content_length()
                    .is_some_and(|size| size > MAX_WIRE)
            {
                return Err(INVALID);
            }
            let mut staged = tempfile::NamedTempFile::new_in(&dir).map_err(|_| STORAGE)?;
            let mut length = 0;
            while let Some(chunk) = response.chunk().await.map_err(|_| NETWORK)? {
                length += chunk.len() as u64;
                if length > MAX_WIRE {
                    return Err(INVALID);
                }
                staged.write_all(&chunk).map_err(|_| STORAGE)?;
            }
            let expected = expected.clone();
            tauri::async_runtime::spawn_blocking(move || {
                verify(staged.reopen().map_err(|_| STORAGE)?, &request, &expected)?;
                staged.as_file().sync_all().map_err(|_| STORAGE)?;
                match staged.persist_noclobber(&target) {
                    Ok(_) => Ok(()),
                    Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {
                        verify_existing(&target, &expected, &request)?
                            .then_some(())
                            .ok_or(STORAGE)
                    }
                    Err(_) => Err(STORAGE),
                }
            })
            .await
            .map_err(|_| STORAGE)??;
        }
        #[cfg(unix)]
        for directory in [&dir, &self.directory] {
            File::open(directory)
                .and_then(|file| file.sync_all())
                .map_err(|_| STORAGE)?;
        }
        Ok(Collected {
            remote_task_id: remote,
            result_sha256: expected,
        })
    }
}

async fn verify_existing_async(path: &Path, digest: &str, spec: &Value) -> Outcome<bool> {
    let (path, digest, spec) = (path.to_owned(), digest.to_owned(), spec.clone());
    tauri::async_runtime::spawn_blocking(move || verify_existing(&path, &digest, &spec))
        .await
        .map_err(|_| STORAGE)?
}

fn verify_existing(path: &Path, digest: &str, spec: &Value) -> Outcome<bool> {
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.is_file() && meta.len() <= MAX_WIRE => {
            verify(File::open(path).map_err(|_| STORAGE)?, spec, digest)?;
            Ok(true)
        }
        Ok(_) => Err(INVALID),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(STORAGE),
    }
}

#[derive(Clone, Copy)]
enum Kind<'a> {
    Json,
    Gif,
    Png(&'a str),
}

fn member<R: Read + Seek>(zip: &mut ZipArchive<R>, name: &str, limit: u64) -> Outcome<Vec<u8>> {
    let entry = zip.by_name(name).map_err(|_| INVALID)?;
    let length = entry.size();
    if entry.name_raw() != name.as_bytes()
        || !entry.is_file()
        || entry.is_symlink()
        || entry
            .unix_mode()
            .is_some_and(|mode| !matches!(mode & 0o170000, 0 | 0o100000))
        || length == 0
        || length > limit
    {
        return Err(INVALID);
    }
    let mut bytes = Vec::new();
    entry
        .take(length + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| INVALID)?;
    if bytes.len() as u64 != length {
        return Err(INVALID);
    }
    Ok(bytes)
}

fn verify(reader: impl Read + Seek, request: &Value, expected: &str) -> Outcome<()> {
    if request["spec"]["operation"] == "identity_candidates" {
        return verify_portraits(reader, portrait_count(request)?, expected, |_, _| ());
    }
    verify_tree(reader, &request["spec"], expected, |_, _, _| Ok(()))
}

pub(super) fn portrait_count(request: &Value) -> Outcome<usize> {
    if request["spec"]["operation"] != "identity_candidates" {
        return Err(INVALID_SELECTION);
    }
    request["candidate_count"]
        .as_u64()
        .filter(|n| (1..=4).contains(n))
        .map(|n| n as usize)
        .ok_or(STORAGE)
}

fn verify_portraits(
    reader: impl Read + Seek,
    count: usize,
    expected: &str,
    mut visit: impl FnMut(usize, &[u8]),
) -> Outcome<()> {
    let mut zip = ZipArchive::new(reader).map_err(|_| INVALID)?;
    if zip.has_duplicate_names() || zip.len() != count + 1 {
        return Err(INVALID);
    }
    let data = member(&mut zip, META, 64 * 1024)?;
    let meta: Value = serde_json::from_slice(&data).map_err(|_| INVALID)?;
    let paths = meta["candidates"].as_array().ok_or(INVALID)?;
    if meta["schema_version"] != 1 || meta["result_kind"] != "candidates" || paths.len() != count {
        return Err(INVALID);
    }
    let mut tree = Sha256::new();
    tree_member(&mut tree, META, &data);
    // Candidate responses may have larger canvases than 512px animation frames.
    // Split the maximum wire size over the small gallery so IPC remains bounded.
    for (i, path) in paths.iter().enumerate() {
        let name = format!("candidates/candidate-{:02}.png", i + 1);
        if path.as_str() != Some(name.as_str()) {
            return Err(INVALID);
        }
        let data = member(&mut zip, &name, 16 * 1024 * 1024)?;
        if !valid_portrait_header(&data) {
            return Err(INVALID);
        }
        tree_member(&mut tree, &name, &data);
        visit(i + 1, &data);
    }
    verified_digest(tree, expected)
}

pub(super) fn valid_portrait_header(bytes: &[u8]) -> bool {
    if !bytes.starts_with(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR") || bytes.len() < 33 {
        return false;
    }
    [16, 20]
        .into_iter()
        .all(|n| (1..=4096).contains(&u32::from_be_bytes(bytes[n..n + 4].try_into().unwrap())))
}

fn tree_member(tree: &mut Sha256, name: &str, data: &[u8]) {
    tree.update((name.len() as u32).to_be_bytes());
    tree.update(name.as_bytes());
    tree.update((data.len() as u64).to_be_bytes());
    tree.update(data);
}

fn verified_digest(tree: Sha256, expected: &str) -> Outcome<()> {
    let digest: String = tree
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    if digest != expected {
        return Err(INVALID);
    }
    Ok(())
}

// Visitors may stage bytes, but must not publish them until this returns success.
fn verify_tree(
    reader: impl Read + Seek,
    spec: &Value,
    expected: &str,
    mut visit: impl FnMut(&str, Kind<'_>, &[u8]) -> Outcome<()>,
) -> Outcome<()> {
    let mut zip = ZipArchive::new(reader).map_err(|_| INVALID)?;
    if zip.has_duplicate_names() || zip.is_empty() || zip.len() > 2048 {
        return Err(INVALID);
    }
    let meta: Value =
        serde_json::from_slice(&member(&mut zip, META, 64 * 1024)?).map_err(|_| INVALID)?;
    if meta["schema_version"] != 1
        || meta["result_kind"] != "package"
        || meta["package_root"] != "package"
    {
        return Err(INVALID);
    }
    let manifest: Value =
        serde_json::from_slice(&member(&mut zip, MANIFEST, 512 * 1024)?).map_err(|_| INVALID)?;
    let files = expected_files(&manifest, spec)?;
    if zip.len() != files.len() {
        return Err(INVALID);
    }
    let mut total = 0;
    let mut tree = Sha256::new();
    for (name, kind) in files {
        let limit = match kind {
            Kind::Json => 512 * 1024,
            Kind::Gif => 32 * 1024 * 1024,
            Kind::Png(_) => 4 * 1024 * 1024,
        };
        let data = member(&mut zip, &name, limit.min(MAX_EXPANDED - total))?;
        total += data.len() as u64;
        match kind {
            Kind::Png(digest) if super::digest_hex(&data) != digest || !png_header(&data) => {
                return Err(INVALID)
            }
            Kind::Gif if !data.starts_with(b"GIF89a") && !data.starts_with(b"GIF87a") => {
                return Err(INVALID)
            }
            _ => (),
        }
        // The old HTTP protocol hashes relative name and uncompressed bytes, not the ZIP.
        tree_member(&mut tree, &name, &data);
        visit(&name, kind, &data)?;
    }
    verified_digest(tree, expected)
}

fn png_header(bytes: &[u8]) -> bool {
    bytes.starts_with(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR\0\0\x02\0\0\0\x02\0\x08\x06")
}

fn expected_files<'a>(manifest: &'a Value, spec: &Value) -> Outcome<BTreeMap<String, Kind<'a>>> {
    if manifest["schema_version"] != 2
        || manifest["role_id"] != spec["profile_id"]
        || manifest["profile"]["id"] != spec["profile_id"]
        || manifest["profile"]["identity_sha256"] != spec["identity_sha256"]
        || manifest["profile"]["appearance_revision"] != spec["appearance_revision"]
        || manifest["package_version"] != spec["package_version"]
        || manifest["canvas"] != serde_json::json!([512, 512])
        // Task success delivers a reviewable draft, not user acceptance or installation.
        || !matches!(manifest["quality"]["status"].as_str(), Some("pending" | "accepted"))
        || !manifest["rights"]["output_accepted"].is_boolean()
        || (manifest["quality"]["status"] == "pending" && manifest["rights"]["output_accepted"] != false)
        || !manifest["rights"]["input_rights_confirmed"].is_boolean()
        || (manifest["quality"]["status"] == "accepted" && manifest["rights"]["input_rights_confirmed"] != true)
    {
        return Err(INVALID);
    }
    let actions = manifest["actions"].as_object().ok_or(INVALID)?;
    if !actions.contains_key("idle")
        || !spec["actions"]
            .as_array()
            .ok_or(INVALID)?
            .iter()
            .all(|name| name.as_str().is_some_and(|name| actions.contains_key(name)))
    {
        return Err(INVALID);
    }
    let mut files = BTreeMap::from([(META.into(), Kind::Json), (MANIFEST.into(), Kind::Json)]);
    for (action, definition) in actions {
        let required_frames = match action.as_str() {
            "idle" | "patrol" => 12,
            "dragging" | "drag_pickup" => 18,
            "notice" | "click_reaction" | "welcome" | "peek_left" | "peek_right" | "sit_down"
            | "patrol_look_left" | "patrol_look_right" => 8,
            _ => return Err(INVALID),
        };
        let frames = definition["frames"].as_array().ok_or(INVALID)?;
        if frames.len() != required_frames {
            return Err(INVALID);
        }
        for (index, frame) in frames.iter().enumerate() {
            let relative = format!("actions/{action}/{:04}.png", index + 1);
            let hash = frame["sha256"]
                .as_str()
                .filter(|s| is_hex(s, 64))
                .ok_or(INVALID)?;
            if frame["path"] != relative {
                return Err(INVALID);
            }
            files.insert(format!("package/{relative}"), Kind::Png(hash));
        }
        files.insert(format!("package/previews/{action}.gif"), Kind::Gif);
    }
    Ok(files)
}
