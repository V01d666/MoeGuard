//! Local role transfer. Read only the existing package contract's declared files.
use super::{expected_files, png_header, version_key, Kind, Outcome};
use crate::role_catalog::digest_hex;
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    fs,
    io::{self, Cursor, Read, Seek, Write},
    path::{Path, PathBuf},
};
use zip::{write::SimpleFileOptions, ZipArchive, ZipWriter};

const INVALID: &str = "role_transfer_invalid";
const STORAGE: &str = "role_transfer_storage";
const LIMIT: u64 = 64 * 1024 * 1024;
const MANIFEST_LIMIT: u64 = 512 * 1024;

#[derive(Debug, PartialEq, Eq, Serialize)]
pub(crate) struct Imported {
    pub(crate) key: String,
    pub(crate) name: String,
    version: u64,
}

fn info(manifest: &Value) -> Outcome<Imported> {
    let spec = spec(manifest);
    let (id, version) = version_key(&spec).map_err(|_| INVALID)?;
    let name = manifest["display_name"]
        .as_str()
        .filter(|name| !name.trim().is_empty() && name.chars().count() <= 80)
        .ok_or(INVALID)?;
    if manifest["quality"]["status"] != "accepted"
        || manifest["rights"]["output_accepted"] != true
        || !manifest["profile"]["identity_sha256"]
            .as_str()
            .is_some_and(|hash| super::is_hex(hash, 64))
        || !manifest["profile"]["appearance_revision"]
            .as_u64()
            .is_some_and(|n| n > 0)
        || !manifest["fps"]
            .as_u64()
            .is_some_and(|n| (1..=60).contains(&n))
        || manifest["actions"]["idle"]["loop"] != true
    {
        return Err(INVALID);
    }
    Ok(Imported {
        key: format!("{id}@{version}"),
        name: name.to_owned(),
        version,
    })
}

fn spec(manifest: &Value) -> Value {
    json!({"profile_id":manifest["role_id"], "identity_sha256":manifest["profile"]["identity_sha256"],
           "appearance_revision":manifest["profile"]["appearance_revision"],
           "package_version":manifest["package_version"], "actions":["idle"]})
}

fn safe(meta: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return false;
        }
    }
    !meta.file_type().is_symlink()
}

fn read(reader: impl Read, limit: u64) -> Outcome<Vec<u8>> {
    let mut bytes = Vec::new();
    reader
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| INVALID)?;
    if bytes.is_empty() || bytes.len() as u64 > limit {
        return Err(INVALID);
    }
    Ok(bytes)
}

fn file(path: &Path, limit: u64) -> Outcome<Vec<u8>> {
    let metadata = fs::symlink_metadata(path).map_err(|_| INVALID)?;
    if !safe(&metadata) || !metadata.is_file() || metadata.len() > limit {
        return Err(INVALID);
    }
    read(fs::File::open(path).map_err(|_| INVALID)?, limit)
}

fn directory(root: &Path) -> Outcome<PathBuf> {
    let dir = root.join("portable-roles");
    match fs::symlink_metadata(&dir) {
        Ok(meta) if meta.is_dir() && safe(&meta) => Ok(dir),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(dir),
        _ => Err(STORAGE),
    }
}

fn archive_path(root: &Path, key: &str) -> Outcome<PathBuf> {
    let (id, version) = key.split_once('@').ok_or(INVALID)?;
    let version = version.parse::<u64>().map_err(|_| INVALID)?;
    version_key(&json!({"profile_id":id,"package_version":version})).map_err(|_| INVALID)?;
    if key != format!("{id}@{version}") {
        return Err(INVALID);
    }
    Ok(directory(root)?.join(format!("{key}.moeguard-role")))
}

fn member(zip: &mut ZipArchive<impl Read + Seek>, name: &str, limit: u64) -> Outcome<Vec<u8>> {
    let member = zip.by_name(name).map_err(|_| INVALID)?;
    if !member.is_file() || member.size() > limit {
        return Err(INVALID);
    }
    read(member, limit)
}

fn normalize(mut get: impl FnMut(&str, u64) -> Outcome<Vec<u8>>) -> Outcome<(Imported, Vec<u8>)> {
    let encoded = get("role.json", MANIFEST_LIMIT)?;
    let manifest: Value = serde_json::from_slice(&encoded).map_err(|_| INVALID)?;
    let info = info(&manifest)?;
    let spec = spec(&manifest);
    let expected = expected_files(&manifest, &spec).map_err(|_| INVALID)?;
    let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
    let options = SimpleFileOptions::default();
    writer
        .start_file("role.json", options)
        .map_err(|_| STORAGE)?;
    writer.write_all(&encoded).map_err(|_| STORAGE)?;
    let mut remaining = LIMIT - encoded.len() as u64;
    for (name, kind) in expected {
        let Kind::Png(digest) = kind else {
            continue;
        };
        let name = name.strip_prefix("package/").ok_or(INVALID)?;
        let bytes = get(name, remaining.min(4 * 1024 * 1024))?;
        remaining -= bytes.len() as u64;
        if !png_header(&bytes) || digest_hex(&bytes) != digest {
            return Err(INVALID);
        }
        writer.start_file(name, options).map_err(|_| STORAGE)?;
        writer.write_all(&bytes).map_err(|_| STORAGE)?;
    }
    let bytes = writer.finish().map_err(|_| STORAGE)?.into_inner();
    if bytes.len() as u64 > LIMIT {
        return Err(INVALID);
    }
    Ok((info, bytes))
}

fn publish(root: &Path, (info, bytes): (Imported, Vec<u8>)) -> Outcome<Imported> {
    let target = archive_path(root, &info.key)?;
    let parent = target.parent().ok_or(STORAGE)?;
    fs::create_dir_all(parent).map_err(|_| STORAGE)?;
    let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(|_| STORAGE)?;
    temp.write_all(&bytes)
        .and_then(|_| temp.as_file().sync_all())
        .map_err(|_| STORAGE)?;
    match temp.persist_noclobber(&target) {
        Ok(_) => Ok(info),
        Err(error)
            if error.error.kind() == io::ErrorKind::AlreadyExists
                && file(&target, LIMIT)? == bytes =>
        {
            Ok(info)
        }
        Err(error) if error.error.kind() == io::ErrorKind::AlreadyExists => {
            Err("role_transfer_conflict")
        }
        Err(_) => Err(STORAGE),
    }
}

pub(crate) fn install_zip(root: &Path, reader: impl Read + Seek) -> Outcome<Imported> {
    let mut zip = ZipArchive::new(reader).map_err(|_| INVALID)?;
    if zip.has_duplicate_names() || zip.len() > 2048 {
        return Err(INVALID);
    }
    // Website downloads wrap the same manifest and frames in one top-level directory.
    let all = zip
        .file_names()
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| INVALID)?;
    let names: Vec<_> = all
        .iter()
        .filter(|name| {
            name.as_ref() == "role.json"
                || name.split_once('/').is_some_and(|(prefix, leaf)| {
                    leaf == "role.json"
                        && !prefix.is_empty()
                        && prefix
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
                })
        })
        .collect();
    let [manifest] = names.as_slice() else {
        return Err(INVALID);
    };
    let prefix = manifest
        .strip_suffix("role.json")
        .ok_or(INVALID)?
        .to_owned();
    publish(
        root,
        normalize(|name, limit| member(&mut zip, &format!("{prefix}{name}"), limit))?,
    )
}

pub(crate) fn import(root: &Path, chosen: &Path) -> Outcome<Imported> {
    if chosen.file_name().is_some_and(|name| name == "role.json") {
        let folder = chosen.parent().ok_or(INVALID)?;
        return publish(
            root,
            normalize(|name, limit| {
                let mut path = folder.to_path_buf();
                for component in Path::new(name).components() {
                    let meta = fs::symlink_metadata(&path).map_err(|_| INVALID)?;
                    if !meta.is_dir() || !safe(&meta) {
                        return Err(INVALID);
                    }
                    path.push(component.as_os_str());
                }
                file(&path, limit)
            })?,
        );
    }
    install_zip(root, Cursor::new(file(chosen, LIMIT)?))
}

pub(crate) fn load(root: &Path, key: &str) -> Outcome<Vec<u8>> {
    let bytes = file(&archive_path(root, key)?, LIMIT)?;
    let mut zip = ZipArchive::new(Cursor::new(&bytes)).map_err(|_| INVALID)?;
    if zip.has_duplicate_names() || zip.len() > 2048 {
        return Err(INVALID);
    }
    let (info, validated) = normalize(|name, limit| member(&mut zip, name, limit))?;
    if info.key != key || validated != bytes {
        return Err(INVALID);
    }
    Ok(bytes)
}

pub(crate) fn list(root: &Path) -> Outcome<Vec<Imported>> {
    let mut items = Vec::new();
    let entries = match fs::read_dir(directory(root)?) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(items),
        Err(_) => return Err(STORAGE),
    };
    for entry in entries.take(512).flatten() {
        let filename = entry.file_name();
        let Some(key) = filename
            .to_str()
            .and_then(|s| s.strip_suffix(".moeguard-role"))
        else {
            continue;
        };
        let Ok(path) = archive_path(root, key) else {
            continue;
        };
        let Ok(meta) = fs::symlink_metadata(&path) else {
            continue;
        };
        if !meta.is_file() || !safe(&meta) || meta.len() > LIMIT {
            continue;
        }
        let Ok(file) = fs::File::open(path) else {
            continue;
        };
        let Ok(mut zip) = ZipArchive::new(file) else {
            continue;
        };
        let Ok(bytes) = member(&mut zip, "role.json", MANIFEST_LIMIT) else {
            continue;
        };
        let Ok(value) = serde_json::from_slice(&bytes) else {
            continue;
        };
        if let Ok(item) = info(&value) {
            if item.key == key {
                items.push(item);
            }
        }
    }
    items.sort_by(|a, b| (&a.name, a.version, &a.key).cmp(&(&b.name, b.version, &b.key)));
    Ok(items)
}

#[cfg(windows)]
pub(crate) fn save(path: &Path, bytes: &[u8]) -> Outcome<()> {
    let mut file =
        tempfile::NamedTempFile::new_in(path.parent().ok_or(STORAGE)?).map_err(|_| STORAGE)?;
    file.write_all(bytes)
        .and_then(|_| file.as_file().sync_all())
        .map_err(|_| STORAGE)?;
    file.persist_noclobber(path).map(|_| ()).map_err(|error| {
        if error.error.kind() == io::ErrorKind::AlreadyExists {
            "role_transfer_exists"
        } else {
            STORAGE
        }
    })
}

#[cfg(all(windows, feature = "pet-win32"))]
pub(super) fn frames(root: &Path, key: &str) -> Outcome<crate::pet_frames::Frames> {
    let mut zip = ZipArchive::new(Cursor::new(load(root, key)?)).map_err(|_| INVALID)?;
    let manifest: Value = serde_json::from_slice(&member(&mut zip, "role.json", MANIFEST_LIMIT)?)
        .map_err(|_| INVALID)?;
    let reaction = if manifest["actions"]["click_reaction"].is_object() {
        "click_reaction"
    } else {
        "idle"
    };
    crate::pet_frames::Frames::read_members(reaction, |name, limit| member(&mut zip, name, limit))
        .map_err(|_| INVALID)
}
