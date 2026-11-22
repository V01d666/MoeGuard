//! Local, backward-compatible mailbox markers. The legacy file is read-only.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, Read},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use tempfile::NamedTempFile;

type Outcome<T> = Result<T, &'static str>;
const UNAVAILABLE: &str = "mail_read_unavailable";
const NAME: &str = "mailbox-read.json";
#[cfg(all(feature = "native-tray", any(test, windows)))]
const NOTIFIED: &str = "mailbox-notified.json";
const MAX_BYTES: u64 = 32 * 1024;
const MAX_MARKERS: usize = 200;
const RETENTION: u64 = 30 * 24 * 60 * 60;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Markers {
    schema_version: u8,
    seen: BTreeMap<String, u64>,
}

pub(crate) struct MailReads {
    root: PathBuf,
    legacy: Option<PathBuf>,
}

#[cfg(feature = "native-tray")]
#[derive(Default)]
pub(crate) struct TrayInbox {
    public: Vec<String>,
    private: Vec<String>,
}

#[cfg(feature = "native-tray")]
impl TrayInbox {
    pub(crate) fn replace(&mut self, reply: &Value, public: bool) {
        let (keys, rows) = if public {
            (&mut self.public, reply.as_array())
        } else {
            (&mut self.private, reply["messages"].as_array())
        };
        keys.clear();
        if let Some(rows) = rows {
            keys.extend(rows.iter().filter_map(|row| {
                if !public && row["direction"] != "operator" {
                    return None;
                }
                row["read_key"]
                    .as_str()
                    .filter(|key| valid_key(key))
                    .map(str::to_owned)
            }));
            keys.sort_unstable();
            keys.dedup();
        }
    }
}

impl MailReads {
    pub(crate) fn new(root: PathBuf, legacy_home: Option<&Path>) -> Self {
        Self {
            root,
            legacy: legacy_home.map(|home| {
                home.join(".moeguard/role-workbench/service-client/state")
                    .join(NAME)
            }),
        }
    }

    pub(crate) fn decorate(&self, mut reply: Value, public: bool) -> Value {
        let rows = if public {
            reply.as_array_mut()
        } else {
            reply.get_mut("messages").and_then(Value::as_array_mut)
        };
        let Some(rows) = rows else { return reply };
        let Ok(seen) = self.seen() else { return reply };
        for row in rows {
            if let Some(key) = row["read_key"].as_str() {
                row["unread"] = Value::Bool(!seen.contains_key(key));
            }
        }
        reply
    }

    fn seen(&self) -> Outcome<BTreeMap<String, u64>> {
        let now = clock()?;
        let mut seen = load(&self.root.join(NAME), now).unwrap_or_default();
        if let Some(prior) = self.legacy.as_ref().and_then(|path| load(path, now).ok()) {
            seen.extend(prior);
        }
        Ok(seen)
    }

    #[cfg(feature = "native-tray")]
    pub(crate) fn tray_unread(&self, inbox: &TrayInbox) -> usize {
        self.seen().map_or(0, |seen| {
            inbox
                .public
                .iter()
                .chain(&inbox.private)
                .filter(|&key| !seen.contains_key(key))
                .count()
        })
    }

    #[cfg(all(feature = "native-tray", any(test, windows)))]
    pub(crate) fn claim_notifications(&self, inbox: &TrayInbox) -> Outcome<usize> {
        let seen = self.seen()?;
        let now = clock()?;
        let path = self.root.join(NOTIFIED);
        let mut notified = load(&path, now)?;
        let prior = self
            .legacy
            .as_ref()
            .map(|path| load(&path.with_file_name(NOTIFIED), now))
            .transpose()?
            .unwrap_or_default();
        let fresh: std::collections::BTreeSet<_> = inbox
            .public
            .iter()
            .chain(&inbox.private)
            .filter(|&key| {
                !seen.contains_key(key) && !prior.contains_key(key) && !notified.contains_key(key)
            })
            .collect();
        if fresh.is_empty() || fresh.len() > MAX_MARKERS {
            return Ok(0);
        }
        let mut newest: Vec<_> = notified.into_iter().collect();
        newest.sort_unstable_by(|a, b| (b.1, &b.0).cmp(&(a.1, &a.0)));
        notified = newest.into_iter().take(MAX_MARKERS - fresh.len()).collect();
        notified.extend(fresh.iter().map(|key| ((*key).to_string(), now)));
        self.persist(&path, notified)?;
        Ok(fresh.len())
    }

    pub(crate) fn mark(&self, key: &str) -> Outcome<()> {
        if !valid_key(key) {
            return Err("mail_read_invalid");
        }
        fs::create_dir_all(&self.root).map_err(|_| UNAVAILABLE)?;
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.root.join("mailbox-read.lock"))
            .map_err(|_| UNAVAILABLE)?;
        lock.try_lock().map_err(|_| UNAVAILABLE)?;
        let now = clock()?;
        let path = self.root.join(NAME);
        let mut seen = load(&path, now)?;
        seen.remove(key);
        let mut newest: Vec<_> = seen.into_iter().collect();
        newest.sort_unstable_by(|a, b| (b.1, &b.0).cmp(&(a.1, &a.0)));
        let mut seen: BTreeMap<_, _> = newest.into_iter().take(MAX_MARKERS - 1).collect();
        seen.insert(key.into(), now);
        self.persist(&path, seen)
    }

    fn persist(&self, path: &Path, seen: BTreeMap<String, u64>) -> Outcome<()> {
        let markers = Markers {
            schema_version: 1,
            seen,
        };
        fs::create_dir_all(&self.root).map_err(|_| UNAVAILABLE)?;
        let mut file = NamedTempFile::new_in(&self.root).map_err(|_| UNAVAILABLE)?;
        serde_json::to_writer(file.as_file_mut(), &markers).map_err(|_| UNAVAILABLE)?;
        file.as_file().sync_all().map_err(|_| UNAVAILABLE)?;
        file.persist(path).map_err(|_| UNAVAILABLE)?;
        Ok(())
    }
}

fn clock() -> Outcome<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|time| time.as_secs())
        .map_err(|_| UNAVAILABLE)
}

fn valid_key(key: &str) -> bool {
    key.len() == 64
        && key
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn load(path: &Path, now: u64) -> Outcome<BTreeMap<String, u64>> {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Ok(meta) if meta.is_file() && meta.len() <= MAX_BYTES => (),
        _ => return Err(UNAVAILABLE),
    }
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|f| f.take(MAX_BYTES + 1).read_to_end(&mut bytes))
        .map_err(|_| UNAVAILABLE)?;
    if bytes.len() as u64 > MAX_BYTES {
        return Err(UNAVAILABLE);
    }
    let Markers {
        schema_version,
        mut seen,
    } = serde_json::from_slice(&bytes).map_err(|_| UNAVAILABLE)?;
    if schema_version != 1 || seen.len() > MAX_MARKERS || !seen.keys().all(|key| valid_key(key)) {
        return Err(UNAVAILABLE);
    }
    seen.retain(|_, stamp| (now.saturating_sub(RETENTION)..=now + 300).contains(stamp));
    Ok(seen)
}

#[cfg(all(test, feature = "native-tray"))]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn background_mail_notifies_once_without_marking_read_or_rewriting_legacy() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("legacy");
        let legacy = home.join(".moeguard/role-workbench/service-client/state");
        fs::create_dir_all(&legacy).unwrap();
        let announced = "a".repeat(64);
        let reply = "b".repeat(64);
        let seen = "c".repeat(64);
        let prior =
            serde_json::to_vec(&json!({"schema_version":1,"seen":{(&announced):clock().unwrap()}}))
                .unwrap();
        fs::write(legacy.join("mailbox-notified.json"), &prior).unwrap();
        let reads = || MailReads::new(root.path().join("candidate"), Some(&home));
        let mut inbox = TrayInbox::default();
        inbox.replace(&json!([{"read_key":announced},{"read_key":seen}]), true);
        inbox.replace(
            &json!({"messages":[{"read_key":reply,"direction":"operator"}]}),
            false,
        );
        reads().mark(&seen).unwrap();
        assert_eq!(reads().claim_notifications(&inbox).unwrap(), 1);
        assert_eq!(reads().claim_notifications(&inbox).unwrap(), 0);
        assert_eq!(reads().tray_unread(&inbox), 2);
        assert_eq!(
            fs::read(legacy.join("mailbox-notified.json")).unwrap(),
            prior
        );
    }

    #[test]
    fn tray_counts_unread_from_both_sources_and_updates_after_marking() {
        let root = tempfile::tempdir().unwrap();
        let reads = MailReads::new(root.path().into(), None);
        let announcement = "a".repeat(64);
        let reply = "b".repeat(64);
        let public = json!([{"read_key":announcement}]);
        let private =
            json!({"messages":[{"read_key":reply,"direction":"operator"},{"direction":"user"}]});
        let mut inbox = TrayInbox::default();
        inbox.replace(&public, true);
        inbox.replace(&private, false);
        assert_eq!(reads.tray_unread(&inbox), 2);
        reads.mark(&reply).unwrap();
        assert_eq!(reads.tray_unread(&inbox), 1);
        inbox.replace(&Value::Null, false);
        assert_eq!(reads.tray_unread(&inbox), 1);
    }
}
