//! The active role is local playback state, independent of the workbench journal.
use super::{verified_installation, Collected, Outcome, INVALID, STORAGE};
use crate::pet_frames::Frames;
use crate::role_catalog::Catalog;
use serde_json::Value;
use std::{
    fs, io,
    path::{Path, PathBuf},
};

#[cfg(any(windows, test))]
use super::{is_hex, is_remote};
#[cfg(any(windows, test))]
use std::io::{Read, Write};

#[cfg(any(windows, test))]
const SELECTION: &str = "active-pet.json";
#[cfg(any(windows, test))]
const LIMIT: u64 = 8 * 1024;

#[cfg(any(windows, test))]
#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct Selection {
    schema: u8,
    result: Collected,
    spec: Value,
}

#[cfg(any(windows, test))]
#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct CatalogSelection {
    schema: u8,
    catalog_id: String,
}

#[cfg(windows)]
#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct ImportedSelection {
    schema: u8,
    imported_key: String,
}

#[cfg(any(windows, test))]
#[derive(serde::Deserialize, serde::Serialize)]
#[serde(untagged)]
enum SavedSelection {
    Workshop(Selection),
    Catalog(CatalogSelection),
    #[cfg(windows)]
    Imported(ImportedSelection),
}

#[cfg(windows)]
pub(crate) fn workshop_key(profile: &str) -> String {
    crate::role_catalog::entries()
        .iter()
        .find(|entry| entry.role_id == profile)
        .map_or_else(
            || format!("workshop:{profile}"),
            |entry| format!("catalog:{}", entry.id),
        )
}

#[cfg(windows)]
impl SavedSelection {
    fn key(&self) -> String {
        match self {
            Self::Catalog(role) => format!("catalog:{}", role.catalog_id),
            Self::Workshop(role) => {
                workshop_key(role.spec["profile_id"].as_str().unwrap_or_default())
            }
            Self::Imported(role) => format!("import:{}", role.imported_key),
        }
    }
}

#[cfg(windows)]
pub(crate) fn restore_playback_key(directory: &Path) -> Outcome<Option<String>> {
    Ok(read_selection(directory)?.map(|selection| selection.key()))
}

#[cfg(windows)]
pub(crate) struct ReadyRole {
    pub(crate) key: String,
    pub(crate) name: String,
    source: ReadySource,
}

#[cfg(windows)]
enum ReadySource {
    Catalog(String),
    Installed(Collected),
    Imported(String),
}

#[cfg(windows)]
impl ReadyRole {
    pub(crate) fn prepare(self, catalog: &Catalog, directory: PathBuf) -> Outcome<PreparedPet> {
        match self.source {
            ReadySource::Catalog(id) => prepare_catalog_pet(catalog, directory, &id),
            ReadySource::Installed(result) => {
                super::installed_pet(directory, &result.remote_task_id, &result.result_sha256)
            }
            ReadySource::Imported(key) => prepare_imported_pet(directory, &key),
        }
    }
}

#[cfg(windows)]
pub(crate) fn ready_roles(
    catalog: &Catalog,
    directory: &Path,
    state: &Value,
) -> Outcome<Vec<ReadyRole>> {
    let matches_role = |id: &str| {
        state["roles"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|role| role["id"] == id)
    };
    let removed = |id: &str| {
        state["removedRoles"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|role| role == id)
    };
    let installed = super::installed_pets(directory)?;
    let installed = installed.as_array().ok_or(STORAGE)?;
    let installed_source = |item: &Value| -> Outcome<ReadySource> {
        Ok(ReadySource::Installed(Collected {
            remote_task_id: item["remote_task_id"].as_str().ok_or(STORAGE)?.to_owned(),
            result_sha256: item["result_sha256"].as_str().ok_or(STORAGE)?.to_owned(),
        }))
    };
    let with_idle = |item: &Value| {
        item["actions"]
            .as_array()
            .is_some_and(|actions| actions.iter().any(|action| action == "idle"))
    };
    let mut roles = Vec::new();
    for role in catalog
        .inventory()?
        .into_iter()
        .filter(|role| !removed(&role.id))
    {
        let profile = crate::role_catalog::entries()
            .iter()
            .find(|entry| entry.id == role.id)
            .map(|entry| entry.role_id.as_str())
            .ok_or(STORAGE)?;
        let latest = installed
            .iter()
            .find(|item| item["role_id"] == profile && with_idle(item));
        let name = matches_role(&role.id)
            .and_then(|saved| saved["name"].as_str())
            .map_or(role.name, str::to_owned);
        roles.push(ReadyRole {
            key: format!("catalog:{}", role.id),
            name,
            source: match latest {
                Some(item) => installed_source(item)?,
                None => ReadySource::Catalog(role.id),
            },
        });
    }
    for item in installed.iter().filter(|item| with_idle(item)) {
        let id = item["role_id"].as_str().ok_or(STORAGE)?;
        let key = workshop_key(id);
        if removed(id) || !key.starts_with("workshop:") {
            continue;
        }
        let Some(name) = matches_role(id).and_then(|saved| saved["name"].as_str()) else {
            continue;
        };
        roles.push(ReadyRole {
            key,
            name: name.to_owned(),
            source: installed_source(item)?,
        });
    }
    for item in super::portable::list(directory)? {
        roles.push(ReadyRole {
            key: format!("import:{}", item.key),
            name: item.name,
            source: ReadySource::Imported(item.key),
        });
    }
    Ok(roles)
}

#[cfg(windows)]
pub(crate) fn prepare_imported_pet(directory: PathBuf, key: &str) -> Outcome<PreparedPet> {
    let frames = super::portable::frames(&directory, key)?;
    Ok(PreparedPet {
        frames,
        directory,
        selection: SavedSelection::Imported(ImportedSelection {
            schema: 1,
            imported_key: key.to_owned(),
        }),
    })
}

pub(crate) struct PreparedPet {
    pub(crate) frames: Frames,
    #[cfg(any(windows, test))]
    directory: PathBuf,
    #[cfg(any(windows, test))]
    selection: SavedSelection,
}

pub(crate) fn prepare_catalog_pet(
    catalog: &Catalog,
    directory: PathBuf,
    id: &str,
) -> Outcome<PreparedPet> {
    let frames = catalog.pet(id)?;
    #[cfg(any(windows, test))]
    {
        fs::create_dir_all(&directory).map_err(|_| STORAGE)?;
        if !fs::symlink_metadata(&directory).is_ok_and(|meta| meta.is_dir()) {
            return Err(STORAGE);
        }
    }
    #[cfg(not(any(windows, test)))]
    let _ = directory;
    Ok(PreparedPet {
        frames,
        #[cfg(any(windows, test))]
        directory,
        #[cfg(any(windows, test))]
        selection: SavedSelection::Catalog(CatalogSelection {
            schema: 1,
            catalog_id: id.to_owned(),
        }),
    })
}

impl PreparedPet {
    #[cfg(windows)]
    pub(crate) fn playback_key(&self) -> String {
        self.selection.key()
    }

    pub(super) fn verify(directory: PathBuf, result: Collected, spec: Value) -> Outcome<Self> {
        let frames = installed_frames(&directory, &result, &spec)?;
        Ok(Self {
            frames,
            #[cfg(any(windows, test))]
            directory,
            #[cfg(any(windows, test))]
            selection: SavedSelection::Workshop(Selection {
                schema: 1,
                result,
                // Freeze only enough of the manifest contract to verify playback again offline.
                spec: super::playback_spec(&spec),
            }),
        })
    }

    #[cfg(any(windows, test))]
    pub(crate) fn stage(self) -> Outcome<(Frames, impl FnOnce() -> Outcome<()> + Send)> {
        let bytes = serde_json::to_vec(&self.selection).map_err(|_| STORAGE)?;
        if bytes.len() as u64 > LIMIT {
            return Err(STORAGE);
        }
        let mut staged = tempfile::NamedTempFile::new_in(&self.directory).map_err(|_| STORAGE)?;
        staged
            .write_all(&bytes)
            .and_then(|_| staged.as_file().sync_all())
            .map_err(|_| STORAGE)?;
        let destination = self.directory.join(SELECTION);
        // A failed or canceled native replacement drops the unpublished temporary file.
        Ok((self.frames, move || {
            staged
                .persist(&destination)
                .map(|_| ())
                .map_err(|_| STORAGE)
        }))
    }
}

#[cfg(any(windows, test))]
pub(crate) fn restore_pet(catalog: &Catalog, directory: &Path) -> Outcome<Option<Frames>> {
    match read_selection(directory)? {
        None => Ok(None),
        Some(SavedSelection::Workshop(selected)) if selected.schema == 1 => {
            installed_frames(directory, &selected.result, &selected.spec).map(Some)
        }
        Some(SavedSelection::Catalog(selected)) if selected.schema == 1 => {
            catalog.pet(&selected.catalog_id).map(Some)
        }
        #[cfg(windows)]
        Some(SavedSelection::Imported(selected)) if selected.schema == 1 => {
            super::portable::frames(directory, &selected.imported_key).map(Some)
        }
        _ => Err(STORAGE),
    }
}

#[cfg(any(windows, test))]
fn read_selection(directory: &Path) -> Outcome<Option<SavedSelection>> {
    let path = directory.join(SELECTION);
    for (candidate, is_dir) in [(directory, true), (path.as_path(), false)] {
        match fs::symlink_metadata(candidate) {
            Ok(meta)
                if (is_dir && meta.is_dir())
                    || (!is_dir && meta.is_file() && meta.len() <= LIMIT) =>
            {
                ()
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            _ => return Err(STORAGE),
        }
    }
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(LIMIT + 1).read_to_end(&mut bytes))
        .map_err(|_| STORAGE)?;
    if bytes.len() as u64 > LIMIT {
        return Err(STORAGE);
    }
    let selected = serde_json::from_slice(&bytes).map_err(|_| STORAGE)?;
    if let SavedSelection::Workshop(workshop) = &selected {
        if !is_remote(&workshop.result.remote_task_id)
            || !is_hex(&workshop.result.result_sha256, 64)
        {
            return Err(STORAGE);
        }
    }
    Ok(Some(selected))
}

#[cfg(windows)]
pub(crate) fn clear_removed_pet(directory: &Path, removed: &[String]) -> Outcome<bool> {
    if removed.is_empty() {
        return Ok(false);
    }
    let selection = read_selection(directory)?;
    let id = match &selection {
        None => Some("whale"),
        Some(SavedSelection::Catalog(role)) => Some(role.catalog_id.as_str()),
        Some(SavedSelection::Workshop(role)) => role.spec["profile_id"].as_str(),
        Some(SavedSelection::Imported(_)) => None,
    };
    let matches = id.is_some_and(|id| {
        removed.iter().any(|candidate| {
            candidate == id
                || crate::role_catalog::entries()
                    .iter()
                    .any(|entry| entry.id == candidate.as_str() && entry.role_id == id)
        })
    });
    if !matches {
        return Ok(false);
    }
    if selection.is_some() {
        match fs::remove_file(directory.join(SELECTION)) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(STORAGE),
        }
    }
    Ok(true)
}

#[cfg(all(windows, test))]
#[test]
fn settings_choices_contain_playable_pets_and_respect_removed_roles() {
    let temp = tempfile::tempdir().unwrap();
    let catalog = Catalog::production(temp.path());
    let directory = temp.path().join("revision-legacy");
    let imported = super::portable::install_zip(
        &directory,
        std::io::Cursor::new(include_bytes!(
            "../../../test/fixtures/showcase-role.zip"
        )),
    )
    .unwrap();
    let state = serde_json::json!({"roles":[
        {"id":"whale","name":"小鲸"},
        {"id":"pet-unfinished","name":"仅立绘"},
    ]});
    let choices = ready_roles(&catalog, &directory, &state).unwrap();
    assert_eq!(choices.len(), 2);
    assert_eq!(
        (&choices[0].key[..], &choices[0].name[..]),
        ("catalog:whale", "小鲸")
    );
    assert_eq!(choices[1].key, format!("import:{}", imported.key));
    let imported = choices
        .into_iter()
        .nth(1)
        .unwrap()
        .prepare(&catalog, directory.clone())
        .unwrap();
    assert_eq!(
        imported
            .frames
            .actions
            .iter()
            .find(|action| *action == "idle")
            .map(String::as_str),
        Some("idle")
    );
    let (_, commit) = imported.stage().unwrap();
    commit().unwrap();
    assert!(restore_playback_key(&directory)
        .unwrap()
        .unwrap()
        .starts_with("import:"));
    let state = serde_json::json!({"removedRoles":["whale"]});
    assert_eq!(ready_roles(&catalog, &directory, &state).unwrap().len(), 1);
}

#[cfg(all(windows, test))]
#[test]
fn removing_a_workshop_card_clears_matching_playback_only() {
    let temp = tempfile::tempdir().unwrap();
    let directory = temp.path();
    let selection = directory.join(SELECTION);
    assert!(clear_removed_pet(directory, &["whale".into()]).unwrap());
    fs::write(&selection, br#"{"schema":1,"catalog_id":"arona"}"#).unwrap();
    assert!(!clear_removed_pet(directory, &["whale".into()]).unwrap());
    assert!(selection.is_file());
    assert!(clear_removed_pet(directory, &["arona".into()]).unwrap());
    assert!(!selection.exists());
    let version = serde_json::json!({
        "schema": 1,
        "result": {"remote_task_id": format!("role-{}", "a".repeat(40)), "result_sha256": "b".repeat(64)},
        "spec": {"profile_id": "pet-8af04aee88f590ed"}
    });
    fs::write(&selection, serde_json::to_vec(&version).unwrap()).unwrap();
    assert!(clear_removed_pet(directory, &["whale".into()]).unwrap());
    assert!(!selection.exists());
}

#[cfg(windows)]
pub(crate) fn active_role_archive(catalog: &Catalog, directory: &Path) -> Outcome<Vec<u8>> {
    match read_selection(directory)? {
        None => catalog.source_for_transfer("whale"),
        Some(SavedSelection::Catalog(selected)) if selected.schema == 1 => {
            catalog.source_for_transfer(&selected.catalog_id)
        }
        Some(SavedSelection::Imported(selected)) if selected.schema == 1 => {
            super::portable::load(directory, &selected.imported_key)
        }
        Some(SavedSelection::Workshop(selected)) if selected.schema == 1 => {
            verified_installation(directory, &selected.result, &selected.spec)?;
            let stage = tempfile::tempdir().map_err(|_| STORAGE)?;
            let archive = super::build_archive(
                directory,
                &selected.result,
                &selected.spec,
                stage.path(),
                |_, _| Ok(()),
            )?;
            fs::read(archive.path()).map_err(|_| STORAGE)
        }
        _ => Err(STORAGE),
    }
}

#[cfg(any(windows, test))]
use std::fs::File;

fn installed_frames(directory: &Path, result: &Collected, spec: &Value) -> Outcome<Frames> {
    let staging = verified_installation(directory, result, spec)?;
    // Decode the exact immutable bytes that were compared against the installation.
    let action = spec["actions"][0].as_str().ok_or(INVALID)?;
    Frames::read(staging.path(), action)
}
