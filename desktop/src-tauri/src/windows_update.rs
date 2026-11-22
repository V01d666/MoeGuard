use reqwest::blocking::{Client, Response};
use semver::Version;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::{mpsc::Sender, Mutex},
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager};
use velopack::{
    bundle::Manifest,
    locator::{auto_locate_app_manifest, LocationContext, VelopackLocator},
    sources::UpdateSource,
    Error, UpdateCheck, UpdateInfo, UpdateManager, UpdateOptions, VelopackAsset, VelopackAssetFeed,
};

const FEED: &str = "https://api.moeproject.net/updates/windows/stable";
const PACKAGE_ID: &str = "MoeProject.MoeGuard";
const CHANNEL: &str = "win";
const MAX_FEED_BYTES: u64 = 1024 * 1024;
const MAX_PACKAGE_BYTES: u64 = 1024 * 1024 * 1024;
const CHANGED: &str = "windows-update-changed";

struct StableSource {
    client: Client,
    base: String,
}

impl StableSource {
    fn new() -> Result<Self, Error> {
        Self::at(FEED)
    }

    fn at(base: &str) -> Result<Self, Error> {
        let client = Client::builder()
            .user_agent("MoeGuard-Updater/1")
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .build()
            .map_err(network_error)?;
        Ok(Self {
            client,
            base: base.to_owned(),
        })
    }

    fn get(&self, name: &str, timeout: Duration) -> Result<Response, Error> {
        let response = self
            .client
            .get(format!("{}/{name}", self.base))
            .timeout(timeout)
            .send()
            .map_err(network_error)?;
        if response.status() != reqwest::StatusCode::OK {
            return Err(Error::Other(format!(
                "Update server returned HTTP {}",
                response.status()
            )));
        }
        Ok(response)
    }
}

impl UpdateSource for StableSource {
    fn get_release_feed(
        &self,
        channel: &str,
        app: &Manifest,
        _staged_user_id: &str,
    ) -> Result<VelopackAssetFeed, Error> {
        if channel != CHANNEL || app.id != PACKAGE_ID {
            return Err(Error::Other("Unexpected update channel or app ID".into()));
        }
        let mut bytes = Vec::new();
        self.get("releases.win.json", Duration::from_secs(20))?
            .take(MAX_FEED_BYTES + 1)
            .read_to_end(&mut bytes)?;
        parse_feed(&bytes)
    }

    fn download_release_entry(
        &self,
        asset: &VelopackAsset,
        destination: &Path,
        _progress: Option<Sender<i16>>,
    ) -> Result<(), Error> {
        if !is_stable_package(asset) {
            return Err(Error::InvalidPackage(asset.FileName.clone()));
        }
        let response = self.get(&asset.FileName, Duration::from_secs(20 * 60))?;
        if response
            .content_length()
            .is_some_and(|size| size != asset.Size)
        {
            return Err(Error::InvalidPackage(asset.FileName.clone()));
        }
        let mut file = File::create(destination)?;
        verify_bytes(response, &mut file, asset)?;
        file.sync_all()?;
        Ok(())
    }
}

fn network_error(error: reqwest::Error) -> Error {
    Error::Other(error.to_string())
}

fn is_stable_package(asset: &VelopackAsset) -> bool {
    let Ok(version) = Version::parse(&asset.Version) else {
        return false;
    };
    asset.PackageId == PACKAGE_ID
        && asset.Type == "Full"
        && version.pre.is_empty()
        && version.build.is_empty()
        && asset.FileName == format!("{PACKAGE_ID}-{version}-full.nupkg")
        && (1..=MAX_PACKAGE_BYTES).contains(&asset.Size)
        && asset.SHA256.len() == 64
        && asset.SHA256.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn parse_feed(bytes: &[u8]) -> Result<VelopackAssetFeed, Error> {
    if bytes.len() as u64 > MAX_FEED_BYTES {
        return Err(Error::Other(
            "Update manifest exceeds its size limit".into(),
        ));
    }
    let mut feed: VelopackAssetFeed =
        serde_json::from_slice(bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(bytes))?;
    feed.Assets.retain(is_stable_package);
    if feed.Assets.is_empty() {
        return Err(Error::Other(
            "Update manifest has no valid stable package".into(),
        ));
    }
    Ok(feed)
}

fn hex_digest(digest: &[u8]) -> String {
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn verify_bytes(
    mut input: impl Read,
    mut output: impl Write,
    asset: &VelopackAsset,
) -> Result<(), Error> {
    let mut size = 0;
    let mut hash = Sha256::new();
    let mut buffer = [0; 64 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size += count as u64;
        if size > asset.Size {
            return Err(Error::InvalidPackage(asset.FileName.clone()));
        }
        hash.update(&buffer[..count]);
        output.write_all(&buffer[..count])?;
    }
    if size != asset.Size || !hex_digest(&hash.finalize()).eq_ignore_ascii_case(&asset.SHA256) {
        return Err(Error::InvalidPackage(asset.FileName.clone()));
    }
    Ok(())
}

fn installed() -> Option<VelopackLocator> {
    let locator = auto_locate_app_manifest(LocationContext::FromCurrentExe).ok()?;
    (locator.get_manifest().id == PACKAGE_ID && locator.get_update_path().is_file())
        .then_some(locator)
}

fn options() -> Option<UpdateOptions> {
    Some(UpdateOptions {
        ExplicitChannel: Some(CHANNEL.into()),
        MaximumDeltasBeforeFallback: -1,
        ..UpdateOptions::default()
    })
}

fn manager() -> Result<UpdateManager, Error> {
    UpdateManager::new(StableSource::new()?, options(), None)
}

fn check(manager: &UpdateManager) -> Result<Option<UpdateInfo>, Error> {
    match manager.check_for_updates()? {
        UpdateCheck::UpdateAvailable(offer) => Ok(Some(*offer)),
        UpdateCheck::NoUpdateAvailable => Ok(None),
        UpdateCheck::RemoteIsEmpty => Err(Error::Other("Empty update manifest".into())),
    }
}

fn download(
    manager: &UpdateManager,
    locator: &VelopackLocator,
    offer: &UpdateInfo,
) -> Result<(), Error> {
    let asset = &offer.TargetFullRelease;
    let file = locator.get_packages_dir().join(&asset.FileName);
    // Velopack reuses an existing package. Discard a damaged copy or one left
    // by a previous download of a different build with the same version.
    {
        let _lock = locator.try_get_exclusive_lock()?;
        if file.exists() && verify_bytes(File::open(&file)?, io::sink(), asset).is_err() {
            fs::remove_file(&file)?;
        }
    }
    manager.download_updates(offer, None)?;
    verify_bytes(File::open(file)?, io::sink(), asset)
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "camelCase")]
enum Phase {
    Idle,
    Checking,
    Current,
    Available,
    Downloading,
    CloseWorkshop,
    Failed,
    Restarting,
}

struct Status {
    auto_check: bool,
    phase: Phase,
    offer: Option<UpdateInfo>,
}

struct UpdateState {
    version: String,
    preference: PathBuf,
    status: Mutex<Status>,
    operation: tokio::sync::Mutex<()>,
}

impl UpdateState {
    fn view(&self) -> Value {
        let status = self.status.lock().expect("update status");
        serde_json::json!({
            "version": self.version,
            "autoCheck": status.auto_check,
            "phase": status.phase,
            "available": status.offer.as_ref().map(|offer| &offer.TargetFullRelease.Version),
        })
    }

    fn set_phase(&self, app: &AppHandle, phase: Phase, offer: Option<UpdateInfo>) {
        {
            let mut status = self.status.lock().expect("update status");
            status.phase = phase;
            status.offer = offer;
        }
        let _ = app.emit(CHANGED, self.view());
    }

    fn set_preference(&self, enabled: bool) -> io::Result<()> {
        let parent = self
            .preference
            .parent()
            .expect("update preference directory");
        fs::create_dir_all(parent)?;
        let mut file = tempfile::NamedTempFile::new_in(parent)?;
        file.write_all(if enabled { b"on" } else { b"off" })?;
        file.as_file().sync_all()?;
        file.persist(&self.preference)
            .map_err(|error| error.error)?;
        self.status.lock().expect("update status").auto_check = enabled;
        Ok(())
    }
}

fn auto_check_enabled(preference: &Path, legacy: Option<&Path>) -> bool {
    let mut saved = String::new();
    if File::open(preference)
        .and_then(|file| file.take(4).read_to_string(&mut saved))
        .is_ok()
    {
        match saved.as_str() {
            "on" => return true,
            "off" => return false,
            _ => {}
        }
    }
    legacy
        .and_then(|path| {
            let mut text = String::new();
            File::open(path)
                .ok()?
                .take(MAX_FEED_BYTES)
                .read_to_string(&mut text)
                .ok()?;
            let config: toml::Value = toml::from_str(&text).ok()?;
            config.get("updates")?.get("auto_check_enabled")?.as_bool()
        })
        .unwrap_or(true)
}

pub(crate) fn start(app: &AppHandle, root: &Path) {
    let Some(locator) = installed() else { return };
    let preference = root.join("auto-update-check");
    #[cfg(feature = "legacy-workshop")]
    let legacy = crate::instance_guard::legacy_home()
        .ok()
        .map(|home| home.join(".moeguard/config.toml"));
    #[cfg(not(feature = "legacy-workshop"))]
    let legacy = None::<PathBuf>;
    let auto_check = auto_check_enabled(&preference, legacy.as_deref());
    app.manage(UpdateState {
        version: locator.get_manifest_version().to_string(),
        preference,
        status: Mutex::new(Status {
            auto_check,
            phase: Phase::Idle,
            offer: None,
        }),
        operation: tokio::sync::Mutex::new(()),
    });
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(5)).await;
        let state = app.state::<UpdateState>();
        let Ok(_guard) = state.operation.try_lock() else {
            return;
        };
        let should_check = {
            let status = state.status.lock().expect("update status");
            status.auto_check && matches!(status.phase, Phase::Idle)
        };
        if should_check {
            perform_check(&app, &state, true).await;
        }
    });
}

async fn perform_check(app: &AppHandle, state: &UpdateState, automatic: bool) {
    state.set_phase(app, Phase::Checking, None);
    match tauri::async_runtime::spawn_blocking(|| manager().and_then(|manager| check(&manager)))
        .await
    {
        Ok(Ok(Some(offer))) => {
            let version = offer.TargetFullRelease.Version.clone();
            state.set_phase(app, Phase::Available, Some(offer));
            if automatic {
                notify_available(app, &version);
            }
        }
        Ok(Ok(None)) => state.set_phase(app, Phase::Current, None),
        _ => state.set_phase(app, Phase::Failed, None),
    }
}

fn notify_available(app: &AppHandle, version: &str) {
    #[cfg(all(feature = "native-tray", feature = "legacy-workshop"))]
    if let Ok(language) = app.state::<Mutex<crate::ui_locale::UiLocale>>().lock() {
        let title = language.message("update.title");
        let text = language
            .message("update.notification")
            .replace("{version}", version);
        tauri::async_runtime::spawn_blocking(move || crate::mail_balloon_win32::show(title, text));
    }
    let _ = (app, version);
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Request {
    View,
    Check,
    SetAuto { enabled: bool },
    Apply,
}

pub(crate) async fn request(app: AppHandle, request: Value) -> Result<Option<Value>, &'static str> {
    let Some(state) = app.try_state::<UpdateState>() else {
        return Ok(None);
    };
    let request = serde_json::from_value(request).map_err(|_| "update_invalid_request")?;
    if matches!(request, Request::View) {
        return Ok(Some(state.view()));
    }
    let Ok(_guard) = state.operation.try_lock() else {
        return Ok(Some(state.view()));
    };
    match request {
        Request::View => {}
        Request::Check => perform_check(&app, &state, false).await,
        Request::SetAuto { enabled } => state
            .set_preference(enabled)
            .map_err(|_| "update_pref_failed")?,
        Request::Apply => apply(&app, &state).await,
    }
    Ok(Some(state.view()))
}

async fn apply(app: &AppHandle, state: &UpdateState) {
    let offer = state.status.lock().expect("update status").offer.clone();
    let Some(offer) = offer else { return };
    if app.get_webview_window("workshop").is_some() {
        state.set_phase(app, Phase::CloseWorkshop, Some(offer));
        return;
    }
    let expected = offer.TargetFullRelease;
    state.set_phase(app, Phase::Downloading, None);
    let prepared = tauri::async_runtime::spawn_blocking(move || {
        let locator = installed().ok_or_else(|| Error::Other("Installation unavailable".into()))?;
        let manager = manager()?;
        let offer = check(&manager)?;
        if let Some(ref offer) = offer {
            let asset = &offer.TargetFullRelease;
            if asset.Version == expected.Version
                && asset.Size == expected.Size
                && asset.SHA256.eq_ignore_ascii_case(&expected.SHA256)
            {
                download(&manager, &locator, offer)?;
                return Ok::<_, Error>((Some(manager), Some(offer.clone())));
            }
        }
        Ok((None, offer))
    })
    .await;
    let Ok(Ok((manager, offer))) = prepared else {
        state.set_phase(app, Phase::Failed, None);
        return;
    };
    let Some(offer) = offer else {
        state.set_phase(app, Phase::Current, None);
        return;
    };
    let Some(manager) = manager else {
        state.set_phase(app, Phase::Available, Some(offer));
        return;
    };
    let opening = app.state::<crate::WorkshopWindow>();
    let _opening = opening.0.lock().await;
    if app.get_webview_window("workshop").is_some() {
        state.set_phase(app, Phase::CloseWorkshop, Some(offer));
        return;
    }
    let started = tauri::async_runtime::spawn_blocking(move || {
        manager.wait_exit_then_apply_updates(&offer.TargetFullRelease, false, true, [] as [&str; 0])
    })
    .await;
    if matches!(started, Ok(Ok(()))) {
        state.set_phase(app, Phase::Restarting, None);
        app.exit(0);
    } else {
        state.set_phase(app, Phase::Failed, None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn asset() -> VelopackAsset {
        VelopackAsset {
            PackageId: PACKAGE_ID.into(),
            Type: "Full".into(),
            Version: "0.6.1".into(),
            FileName: format!("{PACKAGE_ID}-0.6.1-full.nupkg"),
            SHA256: hex_digest(&Sha256::digest(b"package")).to_uppercase(),
            Size: 7,
            ..VelopackAsset::default()
        }
    }

    #[test]
    fn stable_feed_accepts_matching_full_packages_only() {
        let valid = asset();
        let mut foreign = asset();
        foreign.PackageId = "Other.App".into();
        let mut traversal = asset();
        traversal.FileName = "../MoeGuard.nupkg".into();
        let mut delta = asset();
        delta.Type = "Delta".into();
        let mut prerelease = asset();
        prerelease.Version = "0.7.0-beta".into();
        prerelease.FileName = format!("{PACKAGE_ID}-0.7.0-beta-full.nupkg");
        let input = serde_json::to_vec(&VelopackAssetFeed {
            Assets: vec![valid, foreign, traversal, delta, prerelease],
        })
        .unwrap();
        let feed = parse_feed(&input).unwrap();
        assert_eq!(feed.Assets.len(), 1);
        assert_eq!(feed.Assets[0].Version, "0.6.1");
        assert!(parse_feed(br#"{"Assets":[]}"#).is_err());
        assert!(parse_feed(&vec![b' '; MAX_FEED_BYTES as usize + 1]).is_err());
    }

    #[test]
    fn package_verification_rejects_wrong_size_and_same_size_corruption() {
        let valid = asset();
        let mut verified = Vec::new();
        verify_bytes(&b"package"[..], &mut verified, &valid).unwrap();
        assert_eq!(verified, b"package");
        for broken in [&b"packag"[..], &b"packagu"[..], &b"package!"[..]] {
            assert!(verify_bytes(broken, io::sink(), &valid).is_err());
        }
    }

    #[test]
    fn startup_check_inherits_legacy_preference_until_changed() {
        let directory = tempfile::tempdir().unwrap();
        let native = directory.path().join("auto-update-check");
        let legacy = directory.path().join("config.toml");
        assert!(auto_check_enabled(&native, Some(&legacy)));
        fs::write(&legacy, "[updates]\nauto_check_enabled = false\n").unwrap();
        assert!(!auto_check_enabled(&native, Some(&legacy)));
        fs::write(&native, "on").unwrap();
        assert!(auto_check_enabled(&native, Some(&legacy)));
        assert!(fs::read_to_string(legacy).unwrap().contains("false"));
    }

    #[test]
    #[ignore = "requires a disposable Velopack install and a local HTTP test feed"]
    fn installed_update_can_check_download_and_stage_next_version() {
        use velopack::locator::create_config_from_root_dir;

        let root = PathBuf::from(std::env::var_os("MG_UPDATE_TEST_INSTALL").unwrap());
        assert!(root.join("isolated-update-test").is_file());
        assert!(root.join(".portable").is_file());
        let feed = std::env::var("MG_UPDATE_TEST_FEED").unwrap();
        let url = reqwest::Url::parse(&feed).unwrap();
        assert_eq!(url.scheme(), "http");
        assert_eq!(url.host_str(), Some("127.0.0.1"));
        let config = create_config_from_root_dir(root);
        let locator = VelopackLocator::new(&config).unwrap();
        let manager =
            UpdateManager::new(StableSource::at(&feed).unwrap(), options(), Some(config)).unwrap();
        assert_eq!(manager.get_current_version_as_string(), "0.6.0");
        let offer = check(&manager).unwrap().unwrap();
        assert_eq!(offer.TargetFullRelease.Version, "0.6.1");
        let file = locator
            .get_packages_dir()
            .join(&offer.TargetFullRelease.FileName);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(file, b"corrupt prior download").unwrap();
        download(&manager, &locator, &offer).unwrap();
        manager
            .wait_exit_then_apply_updates(&offer.TargetFullRelease, true, false, [] as [&str; 0])
            .unwrap();
    }
}
