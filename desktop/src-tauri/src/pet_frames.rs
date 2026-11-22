//! Decode only the validated, immutable frames passed to the native desktop renderer.
use serde_json::Value;
use std::{
    fs::File,
    io::{Cursor, Read},
    path::Path,
};

pub(crate) const SIZE: u32 = 512;
const FRAME_BYTES: usize = (SIZE * SIZE * 4) as usize;
const BAD: &str = "revision_pet_invalid_frames";
type Result<T> = std::result::Result<T, &'static str>;

pub(crate) struct Frames {
    pub(crate) idle: Vec<Vec<u8>>,
    pub(crate) reaction: Vec<Vec<u8>>,
    #[cfg(windows)]
    pub(crate) dragging: Vec<Vec<u8>>,
    #[cfg(windows)]
    pub(crate) peek_left: Vec<Vec<u8>>,
    #[cfg(windows)]
    pub(crate) peek_right: Vec<Vec<u8>>,
    #[cfg(windows)]
    pub(crate) head_body_ratio: f64,
    #[cfg(windows)]
    pub(crate) click_lines: Vec<String>,
    #[cfg(windows)]
    pub(crate) actions: Vec<String>,
    #[cfg(windows)]
    pub(crate) reaction_action: String,
    pub(crate) fps: u64,
}

impl Frames {
    // Input is the new, checked staging directory, exclusively owned by the caller.
    pub(crate) fn read(root: &Path, reaction: &str) -> Result<Self> {
        Self::read_members(reaction, |name, limit| {
            let file = File::open(root.join(name)).map_err(|_| BAD)?;
            read_bytes(file, limit)
        })
    }

    pub(crate) fn read_members(
        reaction: &str,
        mut member: impl FnMut(&str, u64) -> Result<Vec<u8>>,
    ) -> Result<Self> {
        let (manifest, fps) = read_manifest(&mut member)?;
        if manifest["actions"]["idle"]["loop"] != true {
            return Err(BAD);
        }
        #[cfg(windows)]
        let reaction = if manifest["actions"]["click_reaction"].is_object() {
            "click_reaction"
        } else {
            reaction
        };
        let idle = read_action(&manifest, "idle", &mut member)?;
        let reaction_frames = read_action(&manifest, reaction, &mut member)?;
        #[cfg(windows)]
        let mut optional = |key| {
            if manifest["actions"][key].is_object() {
                read_action(&manifest, key, &mut member)
            } else {
                Ok(Vec::new())
            }
        };
        Ok(Self {
            idle,
            reaction: reaction_frames,
            #[cfg(windows)]
            dragging: optional("dragging")?,
            #[cfg(windows)]
            peek_left: optional("peek_left")?,
            #[cfg(windows)]
            peek_right: optional("peek_right")?,
            #[cfg(windows)]
            head_body_ratio: manifest["interaction"]["head_body_ratio"]
                .as_f64()
                .or_else(|| {
                    let reveal = manifest["interaction"]["edge_reveal_fraction"].as_f64()?;
                    (reveal > 0.0).then_some(0.5 / reveal)
                })
                .unwrap_or(3.0)
                .clamp(2.0, 4.0),
            #[cfg(windows)]
            click_lines: manifest["dialogue"]["click_lines"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|line| !line.is_empty() && !line.contains('\0'))
                .take(32)
                .map(|line| line.chars().take(120).collect())
                .collect(),
            #[cfg(windows)]
            actions: manifest["actions"]
                .as_object()
                .ok_or(BAD)?
                .keys()
                .cloned()
                .collect(),
            #[cfg(windows)]
            reaction_action: reaction.to_owned(),
            fps,
        })
    }

    #[cfg(test)]
    pub(crate) fn summary(&self) -> Value {
        serde_json::json!({"idle_frames":self.idle.len(),"reaction_frames":self.reaction.len(),"fps":self.fps})
    }
}

#[cfg(windows)]
pub(crate) fn idle_thumbnail(
    frame: &[u8],
    bounds: windows_sys::Win32::Foundation::RECT,
) -> Result<Vec<u8>> {
    if frame.len() != FRAME_BYTES {
        return Err(BAD);
    }
    let left = (bounds.left - 2).max(0) as usize;
    let top = (bounds.top - 2).max(0) as usize;
    let right = (bounds.right + 2).min(SIZE as i32) as usize;
    let bottom = (bounds.bottom + 2).min(SIZE as i32) as usize;
    let width = right - left;
    let height = bottom - top;
    let step = if width.max(height) > 256 { 2 } else { 1 };
    let width = width.div_ceil(step);
    let height = height.div_ceil(step);
    let mut bytes = Vec::new();
    let mut encoder = png::Encoder::new(&mut bytes, width as u32, height as u32);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_compression(png::Compression::Fast);
    let mut writer = encoder.write_header().map_err(|_| BAD)?;
    let mut pixels = Vec::with_capacity(width * height * 4);
    for row in frame
        .chunks_exact(SIZE as usize * 4)
        .skip(top)
        .take(bottom - top)
        .step_by(step)
    {
        for pixel in row[left * 4..right * 4].chunks_exact(4).step_by(step) {
            pixels.extend_from_slice(pixel);
        }
    }
    writer.write_image_data(&pixels).map_err(|_| BAD)?;
    writer.finish().map_err(|_| BAD)?;
    Ok(bytes)
}

fn read_manifest(member: &mut impl FnMut(&str, u64) -> Result<Vec<u8>>) -> Result<(Value, u64)> {
    let manifest: Value =
        serde_json::from_slice(&member("role.json", 512 * 1024)?).map_err(|_| BAD)?;
    let fps = manifest["fps"]
        .as_u64()
        .filter(|n| (1..=60).contains(n))
        .ok_or(BAD)?;
    Ok((manifest, fps))
}

fn read_action(
    manifest: &Value,
    key: &str,
    member: &mut impl FnMut(&str, u64) -> Result<Vec<u8>>,
) -> Result<Vec<Vec<u8>>> {
    let list = manifest["actions"][key]["frames"]
        .as_array()
        .filter(|frames| (1..=18).contains(&frames.len()))
        .ok_or(BAD)?;
    list.iter()
        .enumerate()
        .map(|(ordinal, frame)| {
            let relative = format!("actions/{key}/{:04}.png", ordinal + 1);
            if frame["path"] != relative {
                return Err(BAD);
            }
            let png = member(&relative, 4 * 1024 * 1024)?;
            let mut decoder = png::Decoder::new(Cursor::new(png))
                .read_info()
                .map_err(|_| BAD)?;
            if decoder.output_buffer_size() != Some(FRAME_BYTES)
                || decoder.info().animation_control.is_some()
            {
                return Err(BAD);
            }
            let mut pixels = vec![0; FRAME_BYTES];
            let info = decoder.next_frame(&mut pixels).map_err(|_| BAD)?;
            if info.width != SIZE
                || info.height != SIZE
                || info.color_type != png::ColorType::Rgba
                || info.bit_depth != png::BitDepth::Eight
            {
                return Err(BAD);
            }
            decoder.finish().map_err(|_| BAD)?;
            Ok(pixels)
        })
        .collect()
}

#[cfg(windows)]
pub(crate) fn action_preview(
    key: &str,
    rate: Option<u8>,
    mut member: impl FnMut(&str, u64) -> Result<Vec<u8>>,
) -> Result<Vec<u8>> {
    if key.is_empty() || key.len() > 32 || !key.bytes().all(|c| c.is_ascii_lowercase() || c == b'_')
    {
        return Err(BAD);
    }
    let (manifest, fps) = read_manifest(&mut member)?;
    let fps = rate.map_or(fps, u64::from);
    let frames = read_action(&manifest, key, &mut member)?;
    let mut bytes = Vec::new();
    let edge = SIZE / 2;
    let mut encoder = png::Encoder::new(&mut bytes, edge, edge);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_compression(png::Compression::Fast);
    if frames.len() > 1 {
        let plays = if manifest["actions"][key]["loop"] == true {
            0
        } else {
            1
        };
        encoder
            .set_animated(frames.len() as u32, plays)
            .map_err(|_| BAD)?;
        encoder.set_frame_delay(1, fps as u16).map_err(|_| BAD)?;
    }
    let mut writer = encoder.write_header().map_err(|_| BAD)?;
    let mut pixels = Vec::with_capacity((edge * edge * 4) as usize);
    for frame in frames {
        pixels.clear();
        for row in frame.chunks_exact(SIZE as usize * 4).step_by(2) {
            for pixel in row.chunks_exact(4).step_by(2) {
                pixels.extend_from_slice(pixel);
            }
        }
        writer.write_image_data(&pixels).map_err(|_| BAD)?;
    }
    writer.finish().map_err(|_| BAD)?;
    Ok(bytes)
}

#[cfg(any(windows, test))]
pub(crate) fn layered_pixels(rgba: &[u8]) -> Vec<u8> {
    rgba.chunks_exact(4)
        .flat_map(|pixel| {
            let alpha = pixel[3];
            let apply = |value: u8| ((u16::from(value) * u16::from(alpha) + 127) / 255) as u8;
            [apply(pixel[2]), apply(pixel[1]), apply(pixel[0]), alpha]
        })
        .collect()
}

pub(crate) fn read_bytes(data: impl Read, limit: u64) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    data.take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| BAD)?;
    if bytes.is_empty() || bytes.len() as u64 > limit {
        return Err(BAD);
    }
    Ok(bytes)
}
