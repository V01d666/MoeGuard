use serde::Deserialize;
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{self, Read, Write},
    path::{Path, PathBuf},
};
use tempfile::NamedTempFile;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Manifest {
    default_locale: String,
    languages: BTreeMap<String, String>,
}

pub struct UiLocale {
    path: PathBuf,
    manifest: Manifest,
    current: String,
}

impl UiLocale {
    pub fn open(root: &Path) -> Self {
        let manifest: Manifest = serde_json::from_str(include_str!("../../i18n/manifest.json"))
            .expect("valid embedded language manifest");
        let path = root.join("ui-locale");
        let mut saved = String::new();
        let recognized = File::open(&path)
            .and_then(|file| file.take(32).read_to_string(&mut saved))
            .is_ok()
            && manifest.languages.contains_key(&saved);
        let current = if recognized {
            saved
        } else {
            manifest.default_locale.clone()
        };
        Self {
            path,
            manifest,
            current,
        }
    }

    pub fn current(&self) -> &str {
        &self.current
    }

    pub fn save(&mut self, locale: String) -> Result<(), &'static str> {
        if !self.manifest.languages.contains_key(&locale) {
            return Err("unsupported_ui_locale");
        }
        if locale != self.current {
            self.persist(&locale)
                .map_err(|_| "ui_locale_write_failed")?;
            self.current = locale;
        }
        Ok(())
    }

    fn persist(&self, locale: &str) -> io::Result<()> {
        let parent = self.path.parent().expect("locale has a parent directory");
        fs::create_dir_all(parent)?;
        let mut file = NamedTempFile::new_in(parent)?;
        file.write_all(locale.as_bytes())?;
        file.as_file().sync_all()?;
        file.persist(&self.path).map_err(|error| error.error)?;
        Ok(())
    }

    pub fn workshop_title(&self) -> String {
        self.message("windowTitle")
    }

    pub fn message(&self, key: &str) -> String {
        let dictionary = match self.current() {
            "en" => include_str!("../../i18n/en.json"),
            _ => include_str!("../../i18n/zh-CN.json"),
        };
        let messages: BTreeMap<&str, &str> =
            serde_json::from_str(dictionary).expect("valid desktop messages");
        messages
            .get(key)
            .expect("desktop message exists")
            .to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn locale_restarts_independently_of_draft_and_task() {
        let root = tempfile::tempdir().unwrap();
        let draft = root.path().join("workshop-checkpoint.json");
        let bytes = br#"{"text":{"name":"keep"},"job":{"id":"same-task"}}"#;
        fs::write(&draft, bytes).unwrap();
        let mut language = UiLocale::open(root.path());
        assert_eq!(language.current(), "zh-CN");
        language.save("en".into()).unwrap();
        assert!(language.save("../en".into()).is_err());
        let loaded = UiLocale::open(root.path());
        assert_eq!(loaded.current(), "en");
        assert_eq!(loaded.workshop_title(), "MoeGuard · Pet Workshop");
        assert_eq!(fs::read(draft).unwrap(), bytes);
    }

    #[test]
    fn unreadable_preference_falls_back_without_changing_bytes() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("ui-locale");
        fs::write(&path, b"unknown").unwrap();
        assert_eq!(UiLocale::open(root.path()).current(), "zh-CN");
        assert_eq!(fs::read(&path).unwrap(), b"unknown");
        fs::remove_file(&path).unwrap();
        fs::create_dir(&path).unwrap();
        let mut language = UiLocale::open(root.path());
        assert!(language.save("en".into()).is_err());
        assert_eq!(language.current(), "zh-CN");
        assert!(path.is_dir());
    }
}
