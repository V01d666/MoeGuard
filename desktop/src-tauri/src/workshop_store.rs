use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use tempfile::NamedTempFile;

// Provisional limit for the offline editor while it still uses inline preview images.
const MAX_BYTES: usize = 128 * 1024 * 1024;
const SCHEMA: u64 = 1;

pub struct WorkshopStore {
    folder: PathBuf,
}

impl WorkshopStore {
    pub fn new(folder: PathBuf) -> Self {
        Self { folder }
    }

    fn snapshot(&self) -> PathBuf {
        self.folder.join("workshop-checkpoint.json")
    }

    fn lock(&self) -> io::Result<File> {
        fs::create_dir_all(&self.folder)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.folder.join("workshop-checkpoint.lock"))?;
        file.lock()?;
        Ok(file)
    }

    pub fn load(&self) -> io::Result<Option<Value>> {
        read_snapshot(&self.snapshot())
    }

    pub fn save(&self, expected: u32, state: Value) -> io::Result<u32> {
        if !state.is_object() {
            return Err(invalid("workshop state must be an object"));
        }
        let _lock = self.lock()?;
        let found = self
            .load()?
            .map_or(0, |data| revision(&data).expect("snapshot was checked"));
        if found != expected {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "stale checkpoint",
            ));
        }
        let next = found
            .checked_add(1)
            .ok_or_else(|| invalid("checkpoint overflow"))?;
        let mut file = NamedTempFile::new_in(&self.folder)?;
        let value = json!({"schema": SCHEMA, "revision": next, "state": state});
        serde_json::to_writer(
            BoundedWriter {
                file: file.as_file_mut(),
                remaining: MAX_BYTES,
            },
            &value,
        )
        .map_err(io::Error::other)?;
        file.as_file().sync_all()?;
        // A failed write leaves the previous committed snapshot untouched.
        file.persist(self.snapshot()).map_err(|e| e.error)?;
        Ok(next)
    }
}

struct BoundedWriter<'a> {
    file: &'a mut File,
    remaining: usize,
}

impl Write for BoundedWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.remaining {
            return Err(invalid("checkpoint is too large"));
        }
        let count = self.file.write(bytes)?;
        self.remaining -= count;
        Ok(count)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

fn invalid(label: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, label)
}

fn revision(value: &Value) -> Option<u32> {
    u32::try_from(value.get("revision")?.as_u64()?)
        .ok()
        .filter(|n| *n > 0)
}

fn read_snapshot(path: &Path) -> io::Result<Option<Value>> {
    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut bytes = Vec::new();
    file.take(MAX_BYTES as u64 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > MAX_BYTES {
        return Err(invalid("checkpoint is too large"));
    }
    let decoded: Value = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
    if decoded.get("schema").and_then(Value::as_u64) != Some(SCHEMA)
        || revision(&decoded).is_none()
        || !decoded.get("state").is_some_and(Value::is_object)
    {
        return Err(invalid("checkpoint format is unrecognized"));
    }
    Ok(Some(decoded))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_process_can_load_the_exact_pending_task_and_draft() {
        let temp = tempfile::tempdir().unwrap();
        let first = WorkshopStore::new(temp.path().to_owned());
        assert!(first.load().unwrap().is_none());
        let state =
            json!({"text":{"name":"小鲸"},"job":{"id":"request-original","status":"running"}});
        assert_eq!(first.save(0, state.clone()).unwrap(), 1);
        drop(first);
        let second = WorkshopStore::new(temp.path().to_owned());
        let restored = second.load().unwrap().unwrap();
        assert_eq!(restored["state"], state);
        assert_eq!(second.save(1, json!({"job":null})).unwrap(), 2);
    }

    #[test]
    fn competing_writes_cannot_replace_the_winning_checkpoint() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().to_owned();
        let second = path.clone();
        let a = std::thread::spawn(move || WorkshopStore::new(path).save(0, json!({"name":"a"})));
        let b = std::thread::spawn(move || WorkshopStore::new(second).save(0, json!({"name":"b"})));
        let results = [a.join().unwrap(), b.join().unwrap()];
        assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
        assert_eq!(
            results.iter().flatten().copied().collect::<Vec<_>>(),
            vec![1]
        );
        assert_eq!(
            results
                .iter()
                .filter_map(|r| r.as_ref().err())
                .next()
                .unwrap()
                .kind(),
            io::ErrorKind::AlreadyExists
        );
    }

    #[test]
    fn invalid_state_and_unknown_schema_do_not_overwrite_old_files() {
        let temp = tempfile::tempdir().unwrap();
        let store = WorkshopStore::new(temp.path().to_owned());
        assert_eq!(
            store.save(0, json!(null)).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        let payload = br#"{"schema":2,"revision":1,"state":{"name":"old"}}"#;
        fs::write(store.snapshot(), payload).unwrap();
        assert!(store.save(1, json!({})).is_err());
        assert_eq!(fs::read(store.snapshot()).unwrap(), payload);
    }
}
