//! Native revision bridge for isolated fixtures and the Windows development candidate.
use super::{digest_hex, entries, Catalog, Outcome, BAD, WHALE};
use crate::workshop_store::WorkshopStore;
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::{redirect::Policy, Client, RequestBuilder, Response, StatusCode};
use semver::Version;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Cursor, Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use zeroize::Zeroizing;
use zip::ZipArchive;

#[path = "revision_result.rs"]
mod result;
#[cfg(any(
    test,
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
pub(super) use result::installed_pets;
#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(test, target_os = "linux", feature = "pet-gtk-probe")
))]
pub(crate) use result::restore_pet;
#[cfg(all(windows, feature = "pet-win32"))]
pub(crate) use result::{
    active_role_archive, clear_removed_pet, portable, prepare_imported_pet, ready_roles,
    restore_playback_key, workshop_key,
};
#[cfg(all(windows, feature = "pet-win32"))]
pub(crate) use result::{installed_action_preview, PreparedPet};
#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
pub(super) use result::{installed_pet, prepare_catalog_pet};

const ACTIONS: &[&str] = &[
    "idle",
    "notice",
    "click_reaction",
    "dragging",
    "patrol",
    "welcome",
    "peek_left",
    "peek_right",
    "sit_down",
];
const INVALID: &str = "revision_invalid_selection";
const STORAGE: &str = "revision_storage";
const NETWORK: &str = "revision_network";
const REJECTED: &str = "revision_service_rejected";
const INPUT_REJECTED: &str = "revision_input_rejected";
const CONTENT_NOTICE: &str = "x-moeguard-content-notice";
const RESPONSE: &str = "revision_response_invalid";
const PENDING: &str = "revision_already_pending";
const CREDIT_SHORTFALL: &str = "revision_credits_shortfall";
const PORTRAIT_SHORTFALL: &str = "portrait_credits_shortfall";

fn already_canonical_portrait(bytes: &[u8]) -> bool {
    // Called after the PNG decoder has checked the header and image data.
    if bytes[24..26] != [8, 6] {
        return false;
    }
    let mut remaining = &bytes[8..];
    let mut has_profile = false;
    while let Some(header) = remaining.get(..8) {
        let size = u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
        let Some(next) = size.checked_add(12).and_then(|end| remaining.get(end..)) else {
            return false;
        };
        match &header[4..] {
            b"IHDR" | b"IDAT" => {}
            b"iCCP" if !has_profile => has_profile = true,
            b"IEND" => return size == 0 && next.is_empty(),
            _ => return false,
        }
        remaining = next;
    }
    false
}

fn canonical_portrait(bytes: &[u8]) -> Outcome<Vec<u8>> {
    use png::ColorType;
    if !result::valid_portrait_header(bytes) {
        return Err(INVALID);
    }
    let mut decoder = png::Decoder::new(Cursor::new(bytes));
    decoder.set_transformations(png::Transformations::normalize_to_color8());
    decoder.set_ignore_text_chunk(true);
    decoder.set_ignore_iccp_chunk(true);
    let mut reader = decoder.read_info().map_err(|_| INVALID)?;
    let mut pixels = vec![0; reader.output_buffer_size().ok_or(INVALID)?];
    let info = reader.next_frame(&mut pixels).map_err(|_| INVALID)?;
    reader.finish().map_err(|_| INVALID)?;
    if already_canonical_portrait(bytes) {
        return Ok(bytes.to_vec());
    }
    pixels.truncate(info.buffer_size());
    let rgba = match info.color_type {
        ColorType::Rgba => pixels,
        ColorType::Rgb => pixels
            .chunks_exact(3)
            .flat_map(|p| [p[0], p[1], p[2], 255])
            .collect(),
        ColorType::GrayscaleAlpha => pixels
            .chunks_exact(2)
            .flat_map(|p| [p[0], p[0], p[0], p[1]])
            .collect(),
        ColorType::Grayscale => pixels
            .into_iter()
            .flat_map(|value| [value, value, value, 255])
            .collect(),
        ColorType::Indexed => return Err(INVALID),
    };
    // The service preserves metadata-free RGBA PNG bytes, so their digest can
    // bind both the upload receipt and the task without encoder-dependent drift.
    let mut output = Vec::new();
    let mut encoder = png::Encoder::new(&mut output, info.width, info.height);
    encoder.set_color(ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().map_err(|_| INVALID)?;
    writer.write_image_data(&rgba).map_err(|_| INVALID)?;
    writer.finish().map_err(|_| INVALID)?;
    Ok(output)
}

async fn portrait_upload(png: String) -> Outcome<Vec<u8>> {
    tauri::async_runtime::spawn_blocking(move || {
        canonical_portrait(&STANDARD.decode(png).map_err(|_| INVALID)?)
    })
    .await
    .map_err(|_| STORAGE)?
}

fn save_identity(directory: &Path, digest: &str, bytes: &[u8]) -> Outcome<()> {
    let root = directory.join("identity-images");
    fs::create_dir_all(&root).map_err(|_| STORAGE)?;
    let mut staged = tempfile::NamedTempFile::new_in(&root).map_err(|_| STORAGE)?;
    staged.write_all(bytes).map_err(|_| STORAGE)?;
    staged.as_file().sync_all().map_err(|_| STORAGE)?;
    staged
        .persist(root.join(format!("{digest}.png")))
        .map_err(|_| STORAGE)?;
    Ok(())
}

fn revision_identity(directory: &Path, digest: &str, draft: &str) -> Outcome<Vec<u8>> {
    if !is_hex(digest, 64) {
        return Err(INVALID);
    }
    let saved = directory
        .join("identity-images")
        .join(format!("{digest}.png"));
    if let Ok(file) = File::open(saved) {
        let mut bytes = Vec::new();
        file.take(20 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| STORAGE)?;
        if result::valid_portrait_header(&bytes) && digest_hex(&bytes) == digest {
            return Ok(bytes);
        }
    }
    // Older active roles kept only the draft PNG. Preserve its bound bytes if
    // it already matches, and accept deterministic normalization when it does.
    let bytes = STANDARD.decode(draft).map_err(|_| INVALID)?;
    if result::valid_portrait_header(&bytes) && digest_hex(&bytes) == digest {
        return Ok(bytes);
    }
    let canonical = canonical_portrait(&bytes)?;
    if digest_hex(&canonical) == digest {
        Ok(canonical)
    } else {
        Err(INVALID)
    }
}

fn catalog_bundle(
    catalog: &Catalog,
    entry: &super::Entry,
) -> Outcome<std::borrow::Cow<'static, [u8]>> {
    let bytes = if entry.bundled {
        std::borrow::Cow::Borrowed(WHALE)
    } else {
        std::borrow::Cow::Owned(
            catalog
                .cached_bytes(entry)?
                .ok_or("revision_source_missing")?,
        )
    };
    entry.verify(&bytes)?;
    Ok(bytes)
}

#[cfg(test)]
#[test]
fn portrait_upload_canonicalizes_rgb_with_metadata_and_preserves_alpha() {
    for (color, source, expected) in [
        (png::ColorType::Rgb, &[3, 5, 7][..], [3, 5, 7, 255]),
        (png::ColorType::Rgba, &[3, 5, 7, 82][..], [3, 5, 7, 82]),
    ] {
        let mut original = Vec::new();
        let mut encoder = png::Encoder::new(&mut original, 1, 1);
        encoder.set_color(color);
        encoder
            .add_text_chunk("Comment".into(), "source metadata".into())
            .unwrap();
        let mut writer = encoder.write_header().unwrap();
        writer.write_image_data(source).unwrap();
        writer.finish().unwrap();

        let canonical = canonical_portrait(&original).unwrap();
        let mut offset = 8;
        while offset < canonical.len() {
            let header = &canonical[offset..offset + 8];
            assert!(matches!(&header[4..], b"IHDR" | b"IDAT" | b"IEND"));
            offset += 12 + u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
        }
        let mut decoder = png::Decoder::new(Cursor::new(canonical))
            .read_info()
            .unwrap();
        let mut output = [0; 4];
        assert_eq!(
            decoder.next_frame(&mut output).unwrap().color_type,
            png::ColorType::Rgba
        );
        assert_eq!(output, expected);
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PortraitText {
    name: String,
    kind: String,
    style: String,
    hair: String,
    eyes: String,
    outfit: String,
    accessory: String,
    description: String,
}

impl PortraitText {
    fn request(&self, count: u8, fallback_name: &str) -> Outcome<Value> {
        fn field(value: &str, max: usize) -> Outcome<&str> {
            let value = value.trim();
            if value.chars().count() > max || value.chars().any(char::is_control) {
                return Err(INVALID);
            }
            Ok(value)
        }
        let name = field(&self.name, 24)?;
        let name = if name.is_empty() {
            field(fallback_name, 80)?
        } else {
            name
        };
        let kind = field(&self.kind, 60)?;
        let description = field(&self.description, 480)?;
        if name.is_empty()
            || (kind.is_empty() && description.is_empty())
            || !(1..=4).contains(&count)
        {
            return Err(INVALID);
        }
        let id = format!("pet-{}", uuid::Uuid::new_v4().simple());
        let description = [kind, description]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        let profile = json!({"schema_version":1,"profile_id":id,"appearance_revision":1,
            "display_name":name,"input":{"kind":"text"},"visual":{
                "presentation":"neutral","silhouette":"chibi",
                "description":description,
                "style_and_mood":field(&self.style, 60)?,"hair":field(&self.hair, 60)?,
                "eyes":field(&self.eyes, 60)?,"clothing":field(&self.outfit, 60)?,
                "accessories":field(&self.accessory, 60)?,"negative_description":"",
                "palette":"","face":"","special_features":"","identity_anchors":[]}});
        let input_digest = digest_hex(
            &serde_json::to_vec(&json!({"profile":profile,"candidate_count":count}))
                .map_err(|_| STORAGE)?,
        );
        Ok(
            json!({"schema_version":1,"spec":{"schema_version":1,"operation":"identity_candidates",
            "profile_id":id,"appearance_revision":1,"actions":[],"input_sha256":input_digest},
            "profile":profile,"candidate_count":count,"assets":[],"revision_instruction":"",
            "accepted_direction_sources":[]}),
        )
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Add,
    Modify,
}

pub(super) struct Prepared {
    spec: Value,
    profile: Value,
    instruction: String,
    idle: Vec<u8>,
    source: Vec<u8>,
}

#[cfg(test)]
impl Prepared {
    pub(super) fn instruction(&self) -> &str {
        &self.instruction
    }
    pub(super) fn spec(&self) -> &Value {
        &self.spec
    }
}

fn read_member(zip: &mut ZipArchive<Cursor<&[u8]>>, name: &str, limit: u64) -> Outcome<Vec<u8>> {
    let member = zip.by_name(name).map_err(|_| BAD)?;
    if member.is_dir() || member.size() == 0 || member.size() > limit {
        return Err(BAD);
    }
    let mut bytes = Vec::new();
    member
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| BAD)?;
    if bytes.is_empty() || bytes.len() as u64 > limit {
        return Err(BAD);
    }
    Ok(bytes)
}

pub(super) fn prepare(
    catalog: &Catalog,
    id: &str,
    mode: Mode,
    selected: Vec<String>,
    instruction: &str,
) -> Outcome<Prepared> {
    let entry = entries().iter().find(|e| e.id == id).ok_or(INVALID)?;
    let valid = !selected.is_empty()
        && selected.len() <= ACTIONS.len()
        && selected.iter().enumerate().all(|(i, action)| {
            ACTIONS.contains(&action.as_str())
                && !selected[..i].contains(action)
                && match mode {
                    Mode::Add => !entry.actions.contains(action),
                    Mode::Modify => selected.len() == 1 && entry.actions.contains(action),
                }
        });
    let instruction = instruction.trim();
    if !valid || instruction.chars().count() > 480 {
        return Err(INVALID);
    }
    let bundle = catalog_bundle(catalog, entry)?;
    let mut outer = ZipArchive::new(Cursor::new(bundle.as_ref())).map_err(|_| BAD)?;
    let idle = read_member(&mut outer, "identity.png", 2 * 1024 * 1024)?;
    let profile: Value =
        serde_json::from_slice(&read_member(&mut outer, "profile.json", 64 * 1024)?)
            .map_err(|_| BAD)?;
    let source = read_member(&mut outer, "source.moeguard-role", entry.bytes as u64)?;
    let mut zip = ZipArchive::new(Cursor::new(source.as_slice())).map_err(|_| BAD)?;
    let package: Value = serde_json::from_slice(&read_member(&mut zip, "role.json", 512 * 1024)?)
        .map_err(|_| BAD)?;
    let role_id = package["role_id"].as_str().ok_or(BAD)?;
    let next_version = package["package_version"]
        .as_u64()
        .filter(|v| (1..i32::MAX as u64).contains(v))
        .ok_or(BAD)?
        + 1;
    let appearance = package["profile"]["appearance_revision"]
        .as_u64()
        .filter(|v| (1..=i32::MAX as u64).contains(v))
        .ok_or(BAD)?;
    let identity = digest_hex(&idle);
    let frame = &package["actions"]["idle"]["frames"][0];
    let frame_path = frame["path"].as_str().ok_or(BAD)?;
    if profile["profile_id"] != role_id
        || profile["appearance_revision"] != appearance
        || profile["input"]["kind"] != "image"
        || profile["input"]["media_type"] != "image/png"
        || profile["input"]["sha256"] != identity
        || package["profile"]["id"] != role_id
        || package["profile"]["identity_sha256"] != identity
        || frame["sha256"] != identity
        || read_member(&mut zip, frame_path, 2 * 1024 * 1024)? != idle
    {
        return Err(BAD);
    }
    let actions: Vec<_> = ACTIONS
        .iter()
        .filter(|&&action| selected.iter().any(|s| s == action))
        .collect();
    let spec = json!({"schema_version":1,"operation":"action_revision","profile_id":role_id,
        "appearance_revision":appearance,"package_version":next_version,"identity_sha256":identity,"actions":actions});
    Ok(Prepared {
        spec,
        profile,
        instruction: instruction.into(),
        idle,
        source,
    })
}

#[derive(Serialize, Deserialize, PartialEq, Eq)]
struct AssetRef {
    purpose: String,
    media_type: String,
    sha256: String,
    size_bytes: usize,
    expires_at: u64,
}

#[derive(Serialize, Deserialize)]
struct Frozen {
    owner: String,
    key: String,
    body: String,
    request_digest: String,
    spec_sha256: String,
    remote: Option<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    motion_edit: bool,
    #[serde(default)]
    dismissed: bool,
    #[serde(default)]
    input_rejected: bool,
}

#[derive(Serialize, Deserialize)]
pub struct Snapshot {
    pub remote_task_id: String,
    spec_sha256: String,
    status: String,
    progress: u8,
    result_sha256: String,
}

pub struct EditService {
    http: Client,
    base: String,
    bearer: Zeroizing<String>,
    owner: String,
    account: String,
    legacy: bool,
    journal: WorkshopStore,
    directory: PathBuf,
}

#[derive(Deserialize)]
struct Credits {
    available_units: u64,
    available_t2i_units: u64,
    available_i2v_units: u64,
    available_flexible_units: u64,
    #[serde(skip)]
    portrait_edit: bool,
}

impl Credits {
    fn lanes(&self) -> Outcome<(u64, u64)> {
        let categories = self
            .available_t2i_units
            .checked_add(self.available_i2v_units)
            .and_then(|sum| sum.checked_add(self.available_flexible_units))
            .ok_or(RESPONSE)?;
        // Match the old workbench: either lane may draw from unclassified and flexible units.
        let shared =
            self.available_flexible_units + self.available_units.saturating_sub(categories);
        let art = self.available_t2i_units + shared;
        let motion = self.available_i2v_units + shared;
        if art.max(motion) > (1_u64 << 53) - 1 {
            return Err(RESPONSE);
        }
        Ok((art, motion))
    }
}

impl EditService {
    #[cfg(feature = "revision-loopback")]
    pub fn from_test_env(root: &Path) -> Outcome<Self> {
        let base = loopback_origin()?;
        let bearer = std::env::var("MG_EDIT_TEST_BEARER")
            .ok()
            .filter(|v| {
                (32..=200).contains(&v.len())
                    && v.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            })
            .ok_or("revision_disabled")?;
        // The fixture supplies the account and bearer as one pair from the test service.
        // Never accept these values from WebView requests or enable this source in release builds.
        let account = std::env::var("MG_EDIT_TEST_ACCOUNT").map_err(|_| "revision_disabled")?;
        Self::new(
            root.join("revision-loopback"),
            base,
            &account,
            Zeroizing::new(bearer),
            false,
        )
    }

    fn new(
        directory: PathBuf,
        base: String,
        account: &str,
        bearer: Zeroizing<String>,
        legacy: bool,
    ) -> Outcome<Self> {
        let owner = journal_owner(&base, account).ok_or("revision_disabled")?;
        let http = service_client(&base)?;
        Ok(Self {
            http,
            base,
            bearer,
            owner,
            account: account.into(),
            legacy,
            journal: WorkshopStore::new(directory.clone()),
            directory,
        })
    }

    fn auth_failure(&self, status: StatusCode) -> Outcome<()> {
        if self.legacy && status == StatusCode::UNAUTHORIZED {
            return Err("revision_session_rejected");
        }
        Ok(())
    }

    async fn response<T: DeserializeOwned>(
        &self,
        request: RequestBuilder,
        status: StatusCode,
    ) -> Outcome<T> {
        self.response_with_limit(request, status, 64 * 1024).await
    }

    async fn response_with_limit<T: DeserializeOwned>(
        &self,
        request: RequestBuilder,
        status: StatusCode,
        max_bytes: usize,
    ) -> Outcome<T> {
        let response = request
            .bearer_auth(self.bearer.as_str())
            .send()
            .await
            .map_err(|_| NETWORK)?;
        self.auth_failure(response.status())?;
        if response.status() != status {
            return Err(REJECTED);
        }
        decode_response(response, max_bytes).await
    }

    async fn mailbox(&self) -> Outcome<Value> {
        #[derive(Deserialize, Serialize)]
        struct Thread {
            messages: Vec<Message>,
            limits: Limits,
        }
        #[derive(Deserialize, Serialize)]
        struct Limits {
            max_body_chars: u32,
            daily_limit: u32,
            remaining_today: u32,
        }
        #[derive(Deserialize, Serialize)]
        #[serde(rename_all = "lowercase")]
        enum Direction {
            User,
            Operator,
        }
        #[derive(Deserialize, Serialize)]
        struct Message {
            message_id: String,
            direction: Direction,
            body: String,
            created_at: u64,
            #[serde(skip_deserializing, skip_serializing_if = "Option::is_none")]
            read_key: Option<String>,
        }
        // Match the legacy JSON transport budget for the existing thread endpoint.
        let mut thread: Thread = self
            .response_with_limit(
                self.http.get(format!("{}/v1/mailbox/messages", self.base)),
                StatusCode::OK,
                1024 * 1024,
            )
            .await?;
        if !(1..=500).contains(&thread.limits.max_body_chars)
            || thread.limits.remaining_today > thread.limits.daily_limit
        {
            return Err(RESPONSE);
        }
        for msg in &mut thread.messages {
            if matches!(msg.direction, Direction::Operator) {
                msg.read_key = mail_read_key(&self.account, &msg.message_id, "mm_", msg.created_at);
            }
        }
        Ok(json!({"owner": self.owner, "messages": thread.messages, "limits": thread.limits}))
    }

    async fn send_mail(
        &self,
        owner: &str,
        client_message_id: &str,
        body: &str,
        attach_diagnostics: bool,
    ) -> Outcome<Value> {
        if owner != self.owner {
            return Err("revision_session_mismatch");
        }
        let body = body.replace("\r\n", "\n").replace('\r', "\n");
        let body = body.trim();
        if !client_message_id
            .strip_prefix("cm_")
            .is_some_and(|suffix| is_hex(suffix, 32))
            || body.is_empty()
            || body.chars().count() > 500
            || body.chars().any(|c| {
                (c.is_control() && !matches!(c, '\n' | '\t'))
                    || matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
            })
        {
            return Err("mailbox_invalid_message");
        }
        let mut payload =
            json!({"schema_version":1,"client_message_id":client_message_id,"body":body});
        if attach_diagnostics {
            let architecture = match std::env::consts::ARCH {
                "x86_64" => "x86_64",
                "aarch64" => "arm64",
                _ => "unknown",
            };
            payload["diagnostics"] = json!({"app_version":env!("CARGO_PKG_VERSION"),"windows_major":"unknown",
                "architecture":architecture,"recent_error_codes":[]});
        }
        let response = self
            .http
            .post(format!("{}/v1/mailbox/messages", self.base))
            .bearer_auth(self.bearer.as_str())
            .header("Content-Type", "application/json")
            .body(payload.to_string())
            .send()
            .await
            .map_err(|_| NETWORK)?;
        self.auth_failure(response.status())?;
        if !matches!(response.status(), StatusCode::OK | StatusCode::CREATED) {
            return Err(REJECTED);
        }
        #[derive(Deserialize, Serialize)]
        struct Receipt {
            message_id: String,
            created_at: u64,
            remaining_today: u32,
        }
        let receipt: Receipt = decode_response(response, 64 * 1024).await?;
        if !receipt
            .message_id
            .strip_prefix("mm_")
            .is_some_and(|suffix| is_hex(suffix, 24))
        {
            return Err(RESPONSE);
        }
        serde_json::to_value(receipt).map_err(|_| RESPONSE)
    }

    async fn upload(
        &self,
        purpose: &str,
        media_type: &str,
        data: Vec<u8>,
        expires_at: u64,
    ) -> Outcome<AssetRef> {
        let expected = AssetRef {
            purpose: purpose.into(),
            media_type: media_type.into(),
            sha256: digest_hex(&data),
            size_bytes: data.len(),
            expires_at,
        };
        let response: AssetRef = self
            .response(
                self.http
                    .post(format!("{}/v1/assets", self.base))
                    .header("Content-Type", media_type)
                    .header("X-MoeGuard-Purpose", purpose)
                    .header("X-MoeGuard-Expires-At", expires_at.to_string())
                    .body(data),
                StatusCode::CREATED,
            )
            .await?;
        if response != expected {
            return Err(RESPONSE);
        }
        Ok(response)
    }

    async fn available_motion(&self) -> Outcome<u64> {
        Ok(self.credits().await?.lanes()?.1)
    }

    async fn credits(&self) -> Outcome<Credits> {
        const CAPABILITY: &str = "x-moeguard-portrait-edit";
        let response = self
            .http
            .get(format!("{}/v1/account", self.base))
            .header(CAPABILITY, "1")
            .bearer_auth(self.bearer.as_str())
            .send()
            .await
            .map_err(|_| NETWORK)?;
        self.auth_failure(response.status())?;
        if response.status() != StatusCode::OK {
            return Err(REJECTED);
        }
        let portrait_edit = response
            .headers()
            .get(CAPABILITY)
            .is_some_and(|value| value == "1");
        let mut balance: Credits = decode_response(response, 64 * 1024).await?;
        balance.portrait_edit = portrait_edit;
        Ok(balance)
    }

    fn credit_view(&self, balance: Credits) -> Outcome<Value> {
        let (art, motion) = balance.lanes()?;
        Ok(json!({"owner":self.owner,"available_art":art,"available_motion":motion}))
    }

    async fn redeem_credit(&self, owner: &str, code: &str) -> Outcome<Value> {
        if owner != self.owner {
            return Err("revision_session_mismatch");
        }
        if code.len() > 128 {
            return Err("credit_invalid");
        }
        let normalized: String = code
            .chars()
            .filter(|c| *c != '-' && !c.is_whitespace())
            .map(|c| c.to_ascii_uppercase())
            .collect();
        if !normalized.strip_prefix("MG").is_some_and(|suffix| {
            suffix.len() == 16
                && suffix
                    .bytes()
                    .all(|b| b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789".contains(&b))
        }) {
            return Err("credit_invalid");
        }
        let reply = self
            .http
            .post(format!("{}/v1/credits/redeem", self.base))
            .bearer_auth(self.bearer.as_str())
            .header("Content-Type", "application/json")
            .body(json!({"schema_version":1,"code":normalized}).to_string())
            .send()
            .await
            .map_err(|_| NETWORK)?;
        self.auth_failure(reply.status())?;
        match reply.status() {
            StatusCode::OK => self.credit_view(decode_response(reply, 64 * 1024).await?),
            StatusCode::CONFLICT | StatusCode::BAD_REQUEST => Err("credit_invalid"),
            _ => Err(REJECTED),
        }
    }

    pub(super) async fn inspect(&self) -> Outcome<Value> {
        let credits = self.credits().await?;
        let (art, motion) = credits.lanes()?;
        let mut result = self.inspect_task(motion).await?;
        result["available_art"] = Value::from(art);
        if credits.portrait_edit {
            result["portrait_edit"] = Value::Bool(true);
        }
        if self.legacy {
            result["legacy_session"] = Value::Bool(true);
        }
        Ok(result)
    }

    async fn inspect_task(&self, available: u64) -> Outcome<Value> {
        let frozen = self.load()?;
        let Some((_, frozen)) = frozen else {
            return Ok(json!({"pending":false,"task":null,"available_motion":available}));
        };
        let body: Value = serde_json::from_str(&frozen.body).map_err(|_| STORAGE)?;
        let spec = &body["request"]["spec"];
        if spec["operation"] == "identity_candidates" {
            let candidate_count = result::portrait_count(&body["request"])?;
            let task = if frozen.remote.is_some() {
                Some(self.query_bound(&frozen).await?)
            } else {
                None
            };
            let mut view = json!({
                "pending":true, "task_kind":"portraits", "task":task,
                "candidate_count":candidate_count, "available_motion":available,
            });
            if body["request"]["portrait_edit"] == true {
                view["portrait_edit_task"] = Value::Bool(true);
            }
            return Ok(view);
        }
        if frozen.remote.is_none() {
            return Ok(json!({"pending":true,"task":null,"available_motion":available}));
        }
        let task = self.query_bound(&frozen).await?;
        let reviewed =
            task.status == "succeeded" && self.has_reviewed_archive(&task.remote_task_id);
        let pet_probe = cfg!(any(
            all(windows, feature = "pet-win32"),
            all(feature = "pet-gtk-probe", target_os = "linux")
        ));
        let installed = reviewed && self.has_installed_version(&task.remote_task_id, spec);
        let mut view = json!({"pending":true,"task":task,"actions":spec["actions"],"available_motion":available,"reviewed":reviewed,"installed":installed,"pet_probe":pet_probe});
        view["role_id"] = spec["profile_id"].clone();
        if frozen.motion_edit {
            view["motion_edit"] = Value::Bool(true);
        }
        if spec["operation"] == "initial_package" {
            view["initial_role"] = spec["profile_id"].clone();
        }
        Ok(view)
    }

    fn load(&self) -> Outcome<Option<(u32, Frozen)>> {
        Ok(self
            .load_checkpoint()?
            .filter(|(_, frozen)| !frozen.dismissed && !frozen.input_rejected))
    }

    fn load_checkpoint(&self) -> Outcome<Option<(u32, Frozen)>> {
        self.journal
            .load()
            .map_err(|_| STORAGE)?
            .map(|snapshot| {
                let revision = snapshot["revision"].as_u64().ok_or(STORAGE)? as u32;
                let frozen: Frozen =
                    serde_json::from_value(snapshot["state"].clone()).map_err(|_| STORAGE)?;
                if frozen.owner != self.owner {
                    return Err("revision_session_mismatch");
                }
                if !is_hex(&frozen.key, 64)
                    || !is_hex(&frozen.spec_sha256, 64)
                    || (frozen.dismissed && frozen.remote.is_none())
                    || (frozen.input_rejected && frozen.remote.is_some())
                    || frozen.remote.as_deref().is_some_and(|id| !is_remote(id))
                    || frozen.request_digest
                        != request_digest(&frozen.owner, &frozen.key, &frozen.body)
                {
                    return Err(STORAGE);
                }
                let wire: Value = serde_json::from_str(&frozen.body).map_err(|_| STORAGE)?;
                if spec_digest(&wire["request"]["spec"])? != frozen.spec_sha256 {
                    return Err(STORAGE);
                }
                Ok((revision, frozen))
            })
            .transpose()
    }

    fn save(&self, revision: u32, value: &Frozen) -> Outcome<()> {
        self.journal
            .save(revision, serde_json::to_value(value).map_err(|_| STORAGE)?)
            .map_err(|e| {
                if e.kind() == std::io::ErrorKind::AlreadyExists {
                    PENDING
                } else {
                    STORAGE
                }
            })?;
        self.sync_journal()
    }

    fn sync_journal(&self) -> Outcome<()> {
        // Before a submission (including recovery), make the rename durable on Unix.
        #[cfg(unix)]
        for dir in [&self.directory, self.directory.parent().ok_or(STORAGE)?] {
            std::fs::File::open(dir)
                .and_then(|file| file.sync_all())
                .map_err(|_| STORAGE)?;
        }
        Ok(())
    }

    pub async fn start(
        &self,
        catalog: Arc<Catalog>,
        id: String,
        mode: Mode,
        selected: Vec<String>,
        instruction: String,
    ) -> Outcome<Snapshot> {
        // Keep the checkpoint revision monotonic after dismissal: an older process
        // must not be able to commit against a recycled revision number.
        let revision = self.next_revision()?;
        if selected.is_empty() || selected.len() > ACTIONS.len() {
            return Err(INVALID);
        }
        if self.available_motion().await? < selected.len() as u64 {
            return Err(CREDIT_SHORTFALL);
        }
        let prepared = tauri::async_runtime::spawn_blocking(move || {
            prepare(&catalog, &id, mode, selected, &instruction)
        })
        .await
        .map_err(|_| STORAGE)??;
        self.submit_prepared(revision, prepared, mode).await
    }

    async fn submit_prepared(
        &self,
        revision: u32,
        prepared: Prepared,
        mode: Mode,
    ) -> Outcome<Snapshot> {
        let expires = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| STORAGE)?
            .as_secs()
            + 7 * 24 * 60 * 60;
        let input = self
            .upload("input_image", "image/png", prepared.idle.clone(), expires)
            .await?;
        let identity = self
            .upload("identity_image", "image/png", prepared.idle, expires)
            .await?;
        let source = self
            .upload(
                "source_package",
                "application/vnd.moeguard.role+zip",
                prepared.source,
                expires,
            )
            .await?;
        let request = json!({"schema_version":1,
            "spec":prepared.spec,"profile":prepared.profile,"candidate_count":1,
            "assets":[input,identity,source],"revision_instruction":prepared.instruction,
            "accepted_direction_sources":[]});
        self.submit_frozen(revision, request, matches!(mode, Mode::Modify))
            .await
    }

    fn next_revision(&self) -> Outcome<u32> {
        match self.load_checkpoint()? {
            Some((revision, frozen)) if frozen.dismissed || frozen.input_rejected => Ok(revision),
            Some(_) => Err(PENDING),
            None => Ok(0),
        }
    }

    async fn start_portraits(
        &self,
        text: PortraitText,
        count: u8,
        fallback_name: &str,
    ) -> Outcome<Snapshot> {
        let revision = self.next_revision()?;
        let request = text.request(count, fallback_name)?;
        if self.credits().await?.lanes()?.0 < u64::from(count) {
            return Err(PORTRAIT_SHORTFALL);
        }
        self.submit_frozen(revision, request, false).await
    }

    async fn edit_portrait(
        &self,
        name: String,
        png: String,
        instruction: String,
    ) -> Outcome<Snapshot> {
        let revision = self.next_revision()?;
        let name = name.trim();
        let instruction = instruction.trim();
        if name.is_empty()
            || name.chars().count() > 80
            || name.chars().any(char::is_control)
            || instruction.is_empty()
            || instruction.chars().count() > 60
            || instruction.chars().any(char::is_control)
            || png.len() > 24 * 1024 * 1024
        {
            return Err(INVALID);
        }
        let image = portrait_upload(png).await?;
        let credits = self.credits().await?;
        if !credits.portrait_edit {
            return Err("portrait_edit_unavailable");
        }
        if credits.lanes()?.0 == 0 {
            return Err(PORTRAIT_SHORTFALL);
        }
        let expires = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| STORAGE)?
            .as_secs()
            + 7 * 24 * 60 * 60;
        let input = self
            .upload("input_image", "image/png", image, expires)
            .await?;
        let id = format!("pet-{}", uuid::Uuid::new_v4().simple());
        let request = json!({"schema_version":1,"spec":{"schema_version":1,"operation":"identity_candidates",
            "profile_id":id,"appearance_revision":1,"actions":[],"input_sha256":input.sha256},
            "profile":{"schema_version":1,"profile_id":id,"display_name":name,"appearance_revision":1,
                "input":{"kind":"image","media_type":"image/png","sha256":input.sha256},
                "visual":{"presentation":"neutral","silhouette":"chibi"}},
            "portrait_edit":true,"candidate_count":1,"assets":[input],
            "revision_instruction":instruction,"accepted_direction_sources":[]});
        self.submit_frozen(revision, request, false).await
    }

    async fn start_initial(
        &self,
        id: String,
        name: String,
        selected: Vec<String>,
        png: String,
    ) -> Outcome<Snapshot> {
        let revision = self.next_revision()?;
        let name = name.trim();
        if !(3..=48).contains(&id.len())
            || !id.as_bytes()[0].is_ascii_alphanumeric()
            || !id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            || entries().iter().any(|entry| entry.id == id)
            || name.is_empty()
            || name.chars().count() > 80
            || name.chars().any(char::is_control)
            || !selected.iter().any(|action| action == "idle")
            || selected.len() > ACTIONS.len()
            || selected.iter().enumerate().any(|(i, action)| {
                !ACTIONS.contains(&action.as_str()) || selected[..i].contains(action)
            })
            || png.len() > 24 * 1024 * 1024
        {
            return Err(INVALID);
        }
        if self.directory.join("roles").join(&id).join("1").exists() {
            return Err("revision_install_conflict");
        }
        let image = portrait_upload(png).await?;
        if self.available_motion().await? < selected.len() as u64 {
            return Err(CREDIT_SHORTFALL);
        }
        let expires = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| STORAGE)?
            .as_secs()
            + 7 * 24 * 60 * 60;
        let input = self
            .upload("input_image", "image/png", image.clone(), expires)
            .await?;
        let identity = self
            .upload("identity_image", "image/png", image.clone(), expires)
            .await?;
        let directory = self.directory.clone();
        let digest = identity.sha256.clone();
        tauri::async_runtime::spawn_blocking(move || save_identity(&directory, &digest, &image))
            .await
            .map_err(|_| STORAGE)??;
        let actions: Vec<_> = ACTIONS
            .iter()
            .filter(|&action| selected.iter().any(|s| s == action))
            .collect();
        let request = json!({"schema_version":1,"spec":{"schema_version":1,"operation":"initial_package",
            "profile_id":id,"appearance_revision":1,"package_version":1,"identity_sha256":identity.sha256,"actions":actions},
            "profile":{"schema_version":1,"profile_id":id,"display_name":name,"appearance_revision":1,
                "input":{"kind":"image","media_type":"image/png","sha256":input.sha256},
                "visual":{"presentation":"neutral","silhouette":"chibi"}},
            "candidate_count":1,"assets":[input,identity],"revision_instruction":"","accepted_direction_sources":[]});
        self.submit_frozen(revision, request, false).await
    }

    async fn start_custom_revision(
        &self,
        catalog: Arc<Catalog>,
        choice: CustomChoice,
        mode: Mode,
        instruction: String,
        source_remote: String,
    ) -> Outcome<Snapshot> {
        let revision = self.next_revision()?;
        let CustomChoice {
            id,
            name,
            actions: selected,
            png,
        } = choice;
        let name = name.trim();
        let instruction = instruction.trim();
        if entries().iter().any(|entry| entry.id == id)
            || selected.is_empty()
            || selected.len() > ACTIONS.len()
            || name.is_empty()
            || name.chars().count() > 80
            || name.chars().any(char::is_control)
            || instruction.chars().count() > 480
            || png.len() > 24 * 1024 * 1024
        {
            return Err(INVALID);
        }
        if self.available_motion().await? < selected.len() as u64 {
            return Err(CREDIT_SHORTFALL);
        }
        let directory = self.directory.clone();
        let lookup = id.clone();
        let (previous, source) = tauri::async_runtime::spawn_blocking(move || {
            result::custom_source(&directory, &lookup, &source_remote)
        })
        .await
        .map_err(|_| STORAGE)??;
        let mut zip = ZipArchive::new(Cursor::new(source.as_slice())).map_err(|_| BAD)?;
        let manifest: Value =
            serde_json::from_slice(&read_member(&mut zip, "role.json", 512 * 1024)?)
                .map_err(|_| BAD)?;
        drop(zip);
        let existing = manifest["actions"].as_object().ok_or(INVALID)?;
        if !selected.iter().enumerate().all(|(i, action)| {
            ACTIONS.contains(&action.as_str())
                && !selected[..i].contains(action)
                && match mode {
                    Mode::Add => !existing.contains_key(action),
                    Mode::Modify => selected.len() == 1 && existing.contains_key(action),
                }
        }) {
            return Err(INVALID);
        }
        let identity = previous["identity_sha256"]
            .as_str()
            .ok_or(INVALID)?
            .to_owned();
        let directory = self.directory.clone();
        let digest = identity.clone();
        let stock = entries().iter().find(|entry| entry.role_id == id);
        let idle = tauri::async_runtime::spawn_blocking(move || {
            if let Some(entry) = stock {
                let bundle = catalog_bundle(&catalog, entry)?;
                let mut zip = ZipArchive::new(Cursor::new(bundle.as_ref())).map_err(|_| BAD)?;
                let image = read_member(&mut zip, "identity.png", 2 * 1024 * 1024)?;
                return if digest_hex(&image) == digest {
                    Ok(image)
                } else {
                    Err(BAD)
                };
            }
            revision_identity(&directory, &digest, &png)
        })
        .await
        .map_err(|_| STORAGE)??;
        let version = previous["package_version"]
            .as_u64()
            .filter(|v| *v < i32::MAX as u64)
            .ok_or(INVALID)?
            + 1;
        let appearance = &previous["appearance_revision"];
        let actions: Vec<_> = ACTIONS
            .iter()
            .filter(|&a| selected.iter().any(|s| s == a))
            .collect();
        self.submit_prepared(revision, Prepared {
            spec: json!({"schema_version":1,"operation":"action_revision","profile_id":id,
                "appearance_revision":appearance,"package_version":version,"identity_sha256":identity,"actions":actions}),
            profile: json!({"schema_version":1,"profile_id":id,"display_name":name,"appearance_revision":appearance,
                "input":{"kind":"image","media_type":"image/png","sha256":identity},"visual":{"presentation":"neutral","silhouette":"chibi"}}),
            instruction: instruction.into(), idle, source,
        }, mode).await
    }

    async fn submit_frozen(
        &self,
        revision: u32,
        request: Value,
        motion_edit: bool,
    ) -> Outcome<Snapshot> {
        let spec_sha256 = spec_digest(&request["spec"])?;
        let body = json!({"schema_version":1,"request":request}).to_string();
        let key = digest_hex(uuid::Uuid::new_v4().as_bytes());
        let frozen = Frozen {
            owner: self.owner.clone(),
            request_digest: request_digest(&self.owner, &key, &body),
            key,
            body,
            spec_sha256,
            remote: None,
            motion_edit,
            dismissed: false,
            input_rejected: false,
        };
        self.save(revision, &frozen)?;
        self.resume().await
    }

    async fn dismiss(&self, remote: &str) -> Outcome<()> {
        let (revision, mut frozen) = self.load()?.ok_or("revision_no_pending")?;
        if frozen.remote.as_deref() != Some(remote) {
            return Err(INVALID);
        }
        let task = self.query_bound(&frozen).await?;
        if !matches!(task.status.as_str(), "failed" | "cancelled") {
            return Err("revision_not_ready");
        }
        frozen.dismissed = true;
        self.save(revision, &frozen)
    }

    async fn cancel(&self, remote: &str) -> Outcome<Snapshot> {
        let (_, frozen) = self.load()?.ok_or("revision_no_pending")?;
        if frozen.remote.as_deref() != Some(remote) {
            return Err(INVALID);
        }
        // Sending cancellation never clears the binding or retries a write.
        let snapshot = self
            .response(
                self.http
                    .post(format!("{}/v1/tasks/{remote}/cancel", self.base)),
                StatusCode::OK,
            )
            .await?;
        validate_snapshot(&snapshot, &frozen)?;
        Ok(snapshot)
    }

    async fn finish(&self, remote: &str, digest: &str) -> Outcome<()> {
        let (revision, mut frozen) = self.load()?.ok_or("revision_no_pending")?;
        if frozen.remote.as_deref() != Some(remote) {
            return Err(INVALID);
        }
        self.verify_finished_install(remote, digest).await?;
        frozen.dismissed = true;
        self.save(revision, &frozen)
    }

    async fn save_motion(&self, remote: &str, digest: &str) -> Outcome<()> {
        let (revision, mut frozen) = self.load_checkpoint()?.ok_or("revision_no_pending")?;
        if frozen.remote.as_deref() != Some(remote) || frozen.input_rejected {
            return Err(INVALID);
        }
        if frozen.dismissed {
            return result::check_install_receipt(&self.directory, remote, digest);
        }
        self.install_motion(remote, digest).await?;
        frozen.dismissed = true;
        self.save(revision, &frozen)
    }

    async fn discard_motion_edit(&self, remote: &str, digest: &str) -> Outcome<()> {
        let (revision, mut frozen) = self.load_checkpoint()?.ok_or("revision_no_pending")?;
        if !frozen.motion_edit || frozen.input_rejected || frozen.remote.as_deref() != Some(remote)
        {
            return Err(INVALID);
        }
        let task = self.query_bound(&frozen).await?;
        if task.status != "succeeded" || !is_hex(digest, 64) || task.result_sha256 != digest {
            return Err("revision_review_mismatch");
        }
        let wire: Value = serde_json::from_str(&frozen.body).map_err(|_| STORAGE)?;
        if self.has_installed_version(remote, &wire["request"]["spec"]) {
            return Err("revision_install_conflict");
        }
        if frozen.dismissed {
            return Ok(());
        }
        frozen.dismissed = true;
        self.save(revision, &frozen)
    }

    pub async fn resume(&self) -> Outcome<Snapshot> {
        if !self.directory.is_dir() {
            return Err("revision_no_pending");
        }
        // Across app processes, no second POST may read the old journal while
        // the first reply is being committed as accepted or definitively rejected.
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.directory.join("submission.lock"))
            .map_err(|_| STORAGE)?;
        lock.try_lock().map_err(|_| "revision_busy")?;
        let (revision, mut frozen) = self.load()?.ok_or("revision_no_pending")?;
        if frozen.remote.is_some() {
            return self.query_bound(&frozen).await;
        }
        self.sync_journal()?;
        let response = self
            .http
            .post(format!("{}/v1/tasks", self.base))
            .header("Content-Type", "application/json")
            .header("Idempotency-Key", &frozen.key)
            .header(CONTENT_NOTICE, "1")
            .bearer_auth(self.bearer.as_str())
            .body(frozen.body.clone())
            .send()
            .await
            .map_err(|_| NETWORK)?;
        self.auth_failure(response.status())?;
        if response.status() == StatusCode::UNPROCESSABLE_ENTITY
            && response
                .headers()
                .get(CONTENT_NOTICE)
                .is_some_and(|value| value == "blocked")
        {
            frozen.input_rejected = true;
            self.save(revision, &frozen)?;
            return Err(INPUT_REJECTED);
        }
        // An ambiguous response leaves the exact request and key intact.
        if response.status() != StatusCode::ACCEPTED {
            return Err(REJECTED);
        }
        let snapshot: Snapshot = decode_response(response, 64 * 1024).await?;
        validate_snapshot(&snapshot, &frozen)?;
        frozen.remote = Some(snapshot.remote_task_id.clone());
        self.save(revision, &frozen)?;
        Ok(snapshot)
    }

    async fn query_bound(&self, frozen: &Frozen) -> Outcome<Snapshot> {
        let remote = frozen.remote.as_deref().ok_or("revision_not_ready")?;
        let snapshot = self
            .response(
                self.http.get(format!("{}/v1/tasks/{remote}", self.base)),
                StatusCode::OK,
            )
            .await?;
        validate_snapshot(&snapshot, frozen)?;
        Ok(snapshot)
    }
}

fn service_client(base: &str) -> Outcome<Client> {
    Client::builder()
        .no_proxy()
        .https_only(base.starts_with("https://"))
        .redirect(Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(25))
        .build()
        .map_err(|_| NETWORK)
}

async fn decode_response<T: DeserializeOwned>(mut response: Response, limit: usize) -> Outcome<T> {
    #[derive(Deserialize)]
    struct Envelope<T> {
        schema_version: u32,
        data: T,
    }
    let mut bytes = Zeroizing::new(Vec::new());
    while let Some(chunk) = response.chunk().await.map_err(|_| NETWORK)? {
        if chunk.len() > limit - bytes.len() {
            return Err(RESPONSE);
        }
        bytes.extend_from_slice(&chunk);
    }
    let envelope: Envelope<T> = serde_json::from_slice(&bytes).map_err(|_| RESPONSE)?;
    if envelope.schema_version != 1 {
        return Err(RESPONSE);
    }
    Ok(envelope.data)
}

#[cfg(feature = "native-tray")]
pub(crate) async fn background_mail(root: PathBuf, home: Option<PathBuf>) -> Outcome<Value> {
    // Poll only a ready session. Background mail never renews or enrolls.
    #[cfg(feature = "revision-loopback")]
    let service = {
        let _ = home;
        EditService::from_test_env(&root)?
    };
    #[cfg(all(windows, feature = "legacy-workshop"))]
    let service = {
        use crate::legacy_session::{self, renewal};
        let home = home.ok_or("revision_session_unavailable")?;
        let base = legacy_session::service_origin().map_err(|_| NETWORK)?;
        open_legacy_service(root.join("revision-legacy"), base.into(), move |now| {
            renewal::automatic_allowed(&home)
                .map_err(|_| legacy_session::SessionError::Unavailable)?;
            legacy_session::load_with(&home, now, legacy_session::unprotect_current_user)
        })
        .await?
    };
    service.mailbox().await
}

pub(crate) async fn announcements() -> Outcome<Value> {
    #[cfg(feature = "revision-loopback")]
    let Ok(base) = loopback_origin() else {
        return Ok(Value::Null);
    };
    #[cfg(all(windows, feature = "legacy-workshop"))]
    let base = crate::legacy_session::service_origin().map_err(|_| NETWORK)?;

    #[derive(Deserialize, Serialize)]
    struct Announcement {
        announcement_id: String,
        title: String,
        body: String,
        published_at: u64,
        #[serde(skip_deserializing, skip_serializing_if = "Option::is_none")]
        read_key: Option<String>,
        #[serde(skip_serializing)]
        expires_at: u64,
        #[serde(skip_serializing)]
        min_version: String,
        #[serde(skip_serializing)]
        max_version: String,
    }
    #[derive(Deserialize)]
    struct Listing {
        announcements: Vec<Announcement>,
    }
    // This route is intentionally separate from EditService: it never opens a session
    // or attaches a bearer, even when a private mailbox is available in the same window.
    let response = service_client(&base)?
        .get(format!("{base}/v1/announcements"))
        .send()
        .await
        .map_err(|_| NETWORK)?;
    if response.status() != StatusCode::OK {
        return Err(REJECTED);
    }
    let mut listing: Listing = decode_response(response, 1024 * 1024).await?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| RESPONSE)?
        .as_secs();
    let version = Version::parse(env!("CARGO_PKG_VERSION")).map_err(|_| RESPONSE)?;
    listing.announcements.retain(|row| {
        row.published_at <= now
            && now < row.expires_at
            && (row.min_version.is_empty()
                || Version::parse(&row.min_version)
                    .is_ok_and(|min| !version.cmp_precedence(&min).is_lt()))
            && (row.max_version.is_empty()
                || Version::parse(&row.max_version)
                    .is_ok_and(|max| version.cmp_precedence(&max).is_lt()))
    });
    for row in &mut listing.announcements {
        row.read_key = mail_read_key(
            "announcements",
            &row.announcement_id,
            "ma_",
            row.published_at,
        );
    }
    serde_json::to_value(listing.announcements).map_err(|_| RESPONSE)
}

fn mail_read_key(scope: &str, id: &str, prefix: &str, stamp: u64) -> Option<String> {
    // Same ASCII JSON bytes (including separators) as the old Python read_fingerprint.
    id.strip_prefix(prefix)
        .filter(|suffix| is_hex(suffix, 24))?;
    Some(digest_hex(
        format!("[\"{scope}\", \"{id}\", {stamp}]").as_bytes(),
    ))
}

#[cfg(feature = "revision-loopback")]
fn loopback_origin() -> Outcome<String> {
    let port = std::env::var("MG_EDIT_TEST_PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .filter(|v| *v != 0)
        .ok_or("revision_disabled")?;
    Ok(format!("http://127.0.0.1:{port}"))
}

#[cfg(any(all(windows, feature = "legacy-workshop"), all(test, not(windows))))]
async fn open_legacy_service(
    directory: PathBuf,
    base: String,
    load: impl FnOnce(
            u64,
        )
            -> Result<crate::legacy_session::SessionState, crate::legacy_session::SessionError>
        + Send
        + 'static,
) -> Outcome<EditService> {
    use crate::legacy_session::{SessionError, SessionState};
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "revision_session_unavailable")?
        .as_secs();
    let session = match tauri::async_runtime::spawn_blocking(move || load(now))
        .await
        .map_err(|_| "revision_session_unavailable")?
    {
        Ok(SessionState::Ready(session)) => session,
        Ok(SessionState::Missing) => return Err("revision_session_missing"),
        Ok(SessionState::Expired) => return Err("revision_session_expired"),
        Err(SessionError::Invalid) => return Err("revision_session_invalid"),
        Err(SessionError::Unavailable) => return Err("revision_session_unavailable"),
    };
    let (account, bearer) = session.into_parts();
    EditService::new(directory, base, &account, bearer, true)
}

#[cfg(any(all(windows, feature = "legacy-workshop"), all(test, not(windows))))]
struct LegacyConnection<'a> {
    directory: &'a Path,
    base: &'a str,
    home: &'a Path,
    unprotect: crate::legacy_session::renewal::Protector,
    protect: crate::legacy_session::renewal::Protector,
}

#[cfg(any(all(windows, feature = "legacy-workshop"), all(test, not(windows))))]
impl LegacyConnection<'_> {
    async fn run(&self, catalog: Arc<Catalog>, request: WindowRequest) -> Outcome<Value> {
        use crate::legacy_session::{self, renewal};
        let task_operation = !matches!(
            request,
            WindowRequest::Mailbox
                | WindowRequest::MailboxSend { .. }
                | WindowRequest::CreditBalance
                | WindowRequest::CreditRedeem { .. }
        );
        renewal::automatic_allowed(self.home)?;
        let home = self.home.to_owned();
        let unprotect = self.unprotect;
        let service =
            match open_legacy_service(self.directory.to_owned(), self.base.into(), move |now| {
                legacy_session::load_with(&home, now, unprotect)
            })
            .await
            {
                // Refresh the account first rather than renewing in the middle of these writes.
                Err("revision_session_expired")
                    if !matches!(
                        request,
                        WindowRequest::MailboxSend { .. } | WindowRequest::CreditRedeem { .. }
                    ) =>
                {
                    let service = self.renew(task_operation, None).await?;
                    return dispatch(&service, catalog, request).await;
                }
                other => other?,
            };
        // Only read actions may be repeated after a 401. A mutating operation
        // may already have made progress before a subsequent request failed.
        let retry = match request {
            WindowRequest::Inspect => Some(WindowRequest::Inspect),
            WindowRequest::Mailbox => Some(WindowRequest::Mailbox),
            WindowRequest::CreditBalance => Some(WindowRequest::CreditBalance),
            _ => None,
        };
        let outcome = dispatch(&service, Arc::clone(&catalog), request).await;
        if let (Err("revision_session_rejected"), Some(read)) = (&outcome, retry) {
            let renewed = self.renew(task_operation, Some(&service)).await?;
            return dispatch(&renewed, catalog, read).await;
        }
        outcome
    }

    async fn renew(
        &self,
        task_operation: bool,
        rejected: Option<&EditService>,
    ) -> Outcome<EditService> {
        use crate::legacy_session::renewal;
        let home = self.home.to_owned();
        let unprotect = self.unprotect;
        let protect = self.protect;
        let pending =
            tauri::async_runtime::spawn_blocking(move || renewal::Pending::open(&home, unprotect))
                .await
                .map_err(|_| "revision_session_unavailable")??;
        let clock = || {
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs())
                .map_err(|_| "revision_session_unavailable")
        };
        let (session, expired) = pending.session(clock()?)?;
        let (account, bearer) = session.into_parts();
        let service = EditService::new(
            self.directory.to_owned(),
            self.base.into(),
            &account,
            bearer,
            true,
        )?;
        if rejected.is_some_and(|prior| prior.owner != service.owner) {
            return Err("revision_session_mismatch");
        }
        // Check the original task's account before the renewal has any side effect.
        if task_operation {
            service.load_checkpoint()?;
        }
        // A peer may already have renewed the same account while the failed GET was in flight.
        if !expired && rejected.is_none_or(|prior| prior.bearer.as_str() != service.bearer.as_str())
        {
            return Ok(service);
        }

        const FAILED: &str = "revision_session_renew_failed";
        let wire = pending.request_body()?;
        let response = service
            .http
            .post(format!("{}/v1/enrollments", self.base))
            .header("Content-Type", "application/json")
            .body(wire.to_vec())
            .send()
            .await
            .map_err(|_| FAILED)?;
        drop(wire);
        // An old server rejects the extra required account field. Never retry in register mode.
        if response.status() != StatusCode::OK {
            return Err(FAILED);
        }
        #[derive(Deserialize)]
        struct Renewed {
            account_id: String,
            bearer_token: String,
            expires_at: u64,
        }
        let reply: Renewed = decode_response(response, 4096).await.map_err(|_| FAILED)?;
        let bearer = Zeroizing::new(reply.bearer_token);
        let session = tauri::async_runtime::spawn_blocking(move || {
            pending.commit(
                &reply.account_id,
                &bearer,
                reply.expires_at,
                clock()?,
                protect,
            )
        })
        .await
        .map_err(|_| "revision_session_unavailable")??;
        let (account, bearer) = session.into_parts();
        EditService::new(
            self.directory.to_owned(),
            self.base.into(),
            &account,
            bearer,
            true,
        )
    }
}

fn journal_owner(origin: &str, account: &str) -> Option<String> {
    let account_byte = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit();
    if !(3..=64).contains(&account.len())
        || !account.bytes().next().is_some_and(account_byte)
        || !account.bytes().all(|b| account_byte(b) || b == b'-')
    {
        return None;
    }
    // The normalized origin and the account returned by enrollment stay fixed across tokens.
    Some(digest_hex(
        format!("moeguard-revision-owner/v1\n{origin}\n{account}").as_bytes(),
    ))
}

fn validate_snapshot(snapshot: &Snapshot, frozen: &Frozen) -> Outcome<()> {
    if snapshot.spec_sha256 != frozen.spec_sha256
        || !is_remote(&snapshot.remote_task_id)
        || frozen
            .remote
            .as_ref()
            .is_some_and(|id| id != &snapshot.remote_task_id)
        || snapshot.progress > 100
        || !matches!(
            snapshot.status.as_str(),
            "queued" | "running" | "cancel_requested" | "cancelled" | "failed" | "succeeded"
        )
    {
        return Err(RESPONSE);
    }
    Ok(())
}

fn is_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn is_remote(value: &str) -> bool {
    value
        .strip_prefix("role-")
        .is_some_and(|suffix| is_hex(suffix, 40))
}
fn request_digest(owner: &str, key: &str, body: &str) -> String {
    digest_hex(format!("{owner}\n{key}\n{body}").as_bytes())
}
fn spec_digest(spec: &Value) -> Outcome<String> {
    // The task spec contains only scalars and one array; the legacy fingerprint sorts keys.
    let sorted: BTreeMap<_, _> = spec.as_object().ok_or(STORAGE)?.iter().collect();
    Ok(digest_hex(
        &serde_json::to_vec(&sorted).map_err(|_| STORAGE)?,
    ))
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum WindowRequest {
    Inspect,
    Mailbox,
    CreditBalance,
    CreditRedeem {
        owner: String,
        code: String,
    },
    MailboxSend {
        owner: String,
        client_message_id: String,
        body: String,
        #[serde(default)]
        attach_diagnostics: bool,
    },
    Submit {
        id: String,
        mode: Mode,
        actions: Vec<String>,
        instruction: String,
    },
    SubmitPortraits {
        owner: String,
        text: PortraitText,
        count: u8,
        fallback_name: String,
    },
    EditPortrait {
        owner: String,
        name: String,
        png: String,
        instruction: String,
    },
    SubmitInitial {
        #[serde(flatten)]
        choice: CustomChoice,
        #[serde(default)]
        mode: Option<Mode>,
        #[serde(default)]
        instruction: String,
        source_remote: Option<String>,
    },
    SaveMotion {
        remote: String,
        digest: String,
    },
    DiscardMotionEdit {
        remote: String,
        digest: String,
    },
    Resume,
    Dismiss {
        remote: String,
    },
    Cancel {
        remote: String,
    },
    Finish {
        remote: String,
        digest: String,
    },
    PortraitGallery {
        remote: String,
        digest: String,
    },
    FinishPortraits {
        remote: String,
        digest: String,
    },
    Preview {
        action: String,
    },
    Confirm {
        remote: String,
        digest: String,
    },
    Install {
        remote: String,
        digest: String,
    },
    #[cfg(all(test, feature = "pet-gtk-probe", target_os = "linux"))]
    PreparePet {
        remote: String,
        digest: String,
    },
}

#[derive(Deserialize)]
struct CustomChoice {
    id: String,
    name: String,
    actions: Vec<String>,
    png: String,
}

enum SessionSource {
    #[cfg(feature = "revision-loopback")]
    Fixture(EditService),
    #[cfg(all(windows, feature = "legacy-workshop"))]
    Windows {
        directory: PathBuf,
        home: Option<PathBuf>,
    },
    #[cfg(all(test, not(windows)))]
    LegacyFixture {
        directory: PathBuf,
        home: PathBuf,
        renew: bool,
    },
}

pub(crate) struct RevisionWindow(tokio::sync::Mutex<SessionSource>);

impl RevisionWindow {
    #[cfg(any(
        all(windows, feature = "pet-win32"),
        all(target_os = "linux", feature = "pet-gtk-probe")
    ))]
    pub(crate) async fn pet_library(
        &self,
        app: &tauri::AppHandle,
        catalog: Arc<Catalog>,
        request: Value,
    ) -> Outcome<Value> {
        let source = self.0.try_lock().map_err(|_| "revision_busy")?;
        let directory = match &*source {
            #[cfg(feature = "revision-loopback")]
            SessionSource::Fixture(service) => service.directory.clone(),
            #[cfg(all(windows, feature = "legacy-workshop"))]
            SessionSource::Windows { directory, .. } => directory.clone(),
            #[cfg(all(test, not(windows)))]
            SessionSource::LegacyFixture { directory, .. } => directory.clone(),
        };
        if request["op"] == "pet_library" {
            return tauri::async_runtime::spawn_blocking(move || installed_pets(&directory))
                .await
                .map_err(|_| STORAGE)?;
        }
        let prepared = tauri::async_runtime::spawn_blocking(move || {
            if request["op"] == "enable_catalog_pet" {
                prepare_catalog_pet(&catalog, directory, request["id"].as_str().ok_or(INVALID)?)
            } else {
                installed_pet(
                    directory,
                    request["remote"].as_str().ok_or(INVALID)?,
                    request["digest"].as_str().ok_or(INVALID)?,
                )
            }
        })
        .await
        .map_err(|_| STORAGE)??;
        #[cfg(target_os = "linux")]
        crate::pet_probe::show(app, prepared.frames).await?;
        #[cfg(windows)]
        crate::show_win32_pet(app, prepared).await?;
        Ok(Value::Null)
    }

    #[cfg(any(
        all(windows, feature = "pet-win32"),
        all(feature = "pet-gtk-probe", target_os = "linux")
    ))]
    pub(crate) async fn enable_pet(
        &self,
        app: &tauri::AppHandle,
        remote: &str,
        digest: &str,
    ) -> Outcome<()> {
        let source = self.0.try_lock().map_err(|_| "revision_busy")?;
        let prepared = match &*source {
            #[cfg(feature = "revision-loopback")]
            SessionSource::Fixture(service) => service.prepare_pet(remote, digest).await?,
            #[cfg(all(windows, feature = "legacy-workshop"))]
            SessionSource::Windows { directory, home } => {
                use crate::legacy_session::{self, renewal};
                let home = home.clone().ok_or("revision_session_unavailable")?;
                renewal::automatic_allowed(&home)?;
                let base =
                    legacy_session::service_origin().map_err(|_| "revision_session_invalid")?;
                open_legacy_service(directory.clone(), base.into(), move |now| {
                    legacy_session::load_with(&home, now, legacy_session::unprotect_current_user)
                })
                .await?
                .prepare_pet(remote, digest)
                .await?
            }
            #[cfg(all(test, not(windows)))]
            SessionSource::LegacyFixture { .. } => return Err("revision_disabled"),
        };
        #[cfg(target_os = "linux")]
        return crate::pet_probe::show(app, prepared.frames).await;
        #[cfg(windows)]
        crate::show_win32_pet(app, prepared).await
    }
    #[cfg(feature = "revision-loopback")]
    pub(crate) fn open(root: &Path) -> Option<Self> {
        EditService::from_test_env(root)
            .ok()
            .map(|service| Self(tokio::sync::Mutex::new(SessionSource::Fixture(service))))
    }

    #[cfg(all(windows, feature = "legacy-workshop"))]
    pub(crate) fn legacy(root: &Path, home: Option<PathBuf>) -> Self {
        Self(tokio::sync::Mutex::new(SessionSource::Windows {
            directory: root.join("revision-legacy"),
            home,
        }))
    }

    #[cfg(all(test, not(windows)))]
    pub(super) fn legacy_fixture(root: &Path, renew: bool) -> Self {
        Self(tokio::sync::Mutex::new(SessionSource::LegacyFixture {
            directory: root.join("revision-legacy"),
            home: root.join("home"),
            renew,
        }))
    }

    pub(crate) async fn request(&self, catalog: Arc<Catalog>, request: Value) -> Outcome<Value> {
        let request = serde_json::from_value(request).map_err(|_| INVALID)?;
        let source = self.0.try_lock().map_err(|_| "revision_busy")?;
        match &*source {
            #[cfg(feature = "revision-loopback")]
            SessionSource::Fixture(service) => dispatch(service, catalog, request).await,
            #[cfg(all(windows, feature = "legacy-workshop"))]
            SessionSource::Windows { directory, home } => {
                let home = home.as_deref().ok_or("revision_session_unavailable")?;
                let base = crate::legacy_session::service_origin()
                    .map_err(|_| "revision_session_invalid")?;
                LegacyConnection {
                    directory,
                    base,
                    home,
                    unprotect: crate::legacy_session::unprotect_current_user,
                    protect: crate::legacy_session::protect_current_user,
                }
                .run(catalog, request)
                .await
            }
            #[cfg(all(test, not(windows)))]
            SessionSource::LegacyFixture {
                directory,
                home,
                renew,
            } => {
                use crate::legacy_session::{self, SessionError};
                fn unprotect(ciphertext: &[u8]) -> Result<Zeroizing<Vec<u8>>, SessionError> {
                    let encrypted = ciphertext
                        .strip_prefix(b"test-v1:")
                        .ok_or(SessionError::Invalid)?;
                    Ok(Zeroizing::new(encrypted.iter().rev().copied().collect()))
                }
                fn protect(plaintext: &[u8]) -> Result<Zeroizing<Vec<u8>>, SessionError> {
                    let mut wire = Zeroizing::new(b"test-v1:".to_vec());
                    wire.extend(plaintext.iter().rev());
                    Ok(wire)
                }
                let base = loopback_origin()?;
                if *renew {
                    return LegacyConnection {
                        directory,
                        base: &base,
                        home,
                        unprotect,
                        protect,
                    }
                    .run(catalog, request)
                    .await;
                }
                let home = home.clone();
                let service = open_legacy_service(directory.clone(), base, move |now| {
                    legacy_session::load_with(&home, now, unprotect)
                })
                .await?;
                dispatch(&service, catalog, request).await
            }
        }
    }
}

async fn dispatch(
    service: &EditService,
    catalog: Arc<Catalog>,
    request: WindowRequest,
) -> Outcome<Value> {
    match request {
        WindowRequest::Inspect => service.inspect().await,
        WindowRequest::Mailbox => service.mailbox().await,
        WindowRequest::CreditBalance => service.credit_view(service.credits().await?),
        WindowRequest::CreditRedeem { owner, code } => service.redeem_credit(&owner, &code).await,
        WindowRequest::MailboxSend {
            owner,
            client_message_id,
            body,
            attach_diagnostics,
        } => {
            service
                .send_mail(&owner, &client_message_id, &body, attach_diagnostics)
                .await
        }
        WindowRequest::Submit {
            id,
            mode,
            actions,
            instruction,
        } => {
            service
                .start(catalog, id, mode, actions, instruction)
                .await?;
            service.inspect().await
        }
        WindowRequest::SubmitPortraits {
            owner,
            text,
            count,
            fallback_name,
        } => {
            if owner != service.owner {
                return Err("revision_session_mismatch");
            }
            service.start_portraits(text, count, &fallback_name).await?;
            service.inspect().await
        }
        WindowRequest::EditPortrait {
            owner,
            name,
            png,
            instruction,
        } => {
            if owner != service.owner {
                return Err("revision_session_mismatch");
            }
            service.edit_portrait(name, png, instruction).await?;
            service.inspect().await
        }
        WindowRequest::SubmitInitial {
            choice,
            mode,
            instruction,
            source_remote,
        } => {
            if let Some(mode) = mode {
                service
                    .start_custom_revision(
                        catalog,
                        choice,
                        mode,
                        instruction,
                        source_remote.ok_or(INVALID)?,
                    )
                    .await?;
            } else {
                service
                    .start_initial(choice.id, choice.name, choice.actions, choice.png)
                    .await?;
            }
            service.inspect().await
        }
        WindowRequest::Resume => {
            service.resume().await?;
            service.inspect().await
        }
        WindowRequest::Dismiss { remote } => {
            service.dismiss(&remote).await?;
            Ok(Value::Null)
        }
        WindowRequest::Cancel { remote } => {
            serde_json::to_value(service.cancel(&remote).await?).map_err(|_| RESPONSE)
        }
        WindowRequest::Finish { remote, digest } => {
            service.finish(&remote, &digest).await?;
            Ok(Value::Null)
        }
        WindowRequest::SaveMotion { remote, digest } => {
            service.save_motion(&remote, &digest).await?;
            Ok(Value::Null)
        }
        WindowRequest::DiscardMotionEdit { remote, digest } => {
            service.discard_motion_edit(&remote, &digest).await?;
            Ok(Value::Null)
        }
        WindowRequest::PortraitGallery { remote, digest } => {
            service.portrait_gallery(&remote, &digest).await
        }
        WindowRequest::FinishPortraits { remote, digest } => {
            service.finish_portraits(&remote, &digest).await?;
            Ok(Value::Null)
        }
        WindowRequest::Preview { action } => service.preview(&action).await,
        WindowRequest::Confirm { remote, digest } => {
            serde_json::to_value(service.confirm(&remote, &digest).await?).map_err(|_| RESPONSE)
        }
        WindowRequest::Install { remote, digest } => {
            serde_json::to_value(service.install(&remote, &digest).await?).map_err(|_| RESPONSE)
        }
        #[cfg(all(test, feature = "pet-gtk-probe", target_os = "linux"))]
        WindowRequest::PreparePet { remote, digest } => Ok(service
            .prepare_pet(&remote, &digest)
            .await?
            .frames
            .summary()),
    }
}
