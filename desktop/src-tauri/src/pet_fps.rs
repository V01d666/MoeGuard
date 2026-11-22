//! Local playback preferences. Role archives retain their original frame rate.
use std::{
    collections::BTreeMap,
    fs::File,
    io::{ErrorKind, Read, Write},
    path::Path,
};

const FILE: &str = "pet-action-fps.json";
const LIMIT: u64 = 512 * 1024;
pub(crate) type ActionRates = BTreeMap<String, u8>;
type Rates = BTreeMap<String, ActionRates>;

pub(crate) fn valid(fps: u8) -> bool {
    (1..=60).contains(&fps)
}

fn read(root: &Path) -> Result<Rates, &'static str> {
    let file = match File::open(root.join(FILE)) {
        Ok(file) => file,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Rates::new()),
        Err(_) => return Err("pet_fps_storage"),
    };
    let mut data = Vec::new();
    file.take(LIMIT + 1)
        .read_to_end(&mut data)
        .map_err(|_| "pet_fps_storage")?;
    if data.len() as u64 > LIMIT {
        return Err("pet_fps_storage");
    }
    serde_json::from_slice(&data).map_err(|_| "pet_fps_storage")
}

pub(crate) fn load(root: &Path, role: &str) -> ActionRates {
    let mut rates = read(root)
        .ok()
        .and_then(|mut rates| rates.remove(role))
        .unwrap_or_default();
    rates.retain(|_, fps| valid(*fps));
    rates
}

pub(crate) fn save(root: &Path, role: &str, action: &str, fps: u8) -> Result<(), &'static str> {
    if !valid(fps) {
        return Err("pet_fps_invalid");
    }
    let mut rates = read(root)?;
    rates
        .entry(role.to_owned())
        .or_default()
        .insert(action.to_owned(), fps);
    let data = serde_json::to_vec(&rates).map_err(|_| "pet_fps_storage")?;
    if data.len() as u64 > LIMIT {
        return Err("pet_fps_storage");
    }
    let mut file = tempfile::NamedTempFile::new_in(root).map_err(|_| "pet_fps_storage")?;
    file.write_all(&data)
        .and_then(|_| file.as_file().sync_all())
        .map_err(|_| "pet_fps_storage")?;
    file.persist(root.join(FILE))
        .map_err(|_| "pet_fps_storage")?;
    Ok(())
}

#[cfg(test)]
#[test]
fn action_rates_are_independent_and_reopen_with_the_saved_values() {
    let root = tempfile::tempdir().unwrap();
    let dir = root.path();
    save(dir, "catalog:whale", "idle", 1).unwrap();
    save(dir, "catalog:whale", "click_reaction", 60).unwrap();
    save(dir, "workshop:pet-abcd", "idle", 14).unwrap();
    assert_eq!(
        save(dir, "catalog:whale", "idle", 61),
        Err("pet_fps_invalid")
    );
    assert_eq!(
        load(dir, "catalog:whale").into_iter().collect::<Vec<_>>(),
        vec![("click_reaction".into(), 60), ("idle".into(), 1)]
    );
    assert_eq!(load(dir, "workshop:pet-abcd")["idle"], 14);
}
