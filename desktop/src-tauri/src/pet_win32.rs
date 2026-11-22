//! Native Windows companion surface. All HWND and GDI lifetimes stay on the UI thread.
use crate::{
    pet_fps::{self, ActionRates},
    pet_frames::{idle_thumbnail, layered_pixels, Frames, SIZE},
};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use std::{
    cell::RefCell,
    ffi::c_void,
    fs,
    io::{Read, Write},
    path::PathBuf,
    ptr,
    sync::OnceLock,
};
#[path = "pet_bubble.rs"]
mod bubble;
use windows_sys::Win32::{
    Foundation::{
        GetLastError, ERROR_CLASS_ALREADY_EXISTS, HWND, LPARAM, LRESULT, POINT, RECT,
        SIZE as WinSize, WPARAM,
    },
    Graphics::Gdi::{
        CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GdiFlush, GetMonitorInfoW,
        MonitorFromPoint, SelectObject, AC_SRC_ALPHA, AC_SRC_OVER, BITMAPINFO, BI_RGB,
        BLENDFUNCTION, DIB_RGB_COLORS, HBITMAP, HDC, HGDIOBJ, MONITORINFO,
        MONITOR_DEFAULTTONEAREST, MONITOR_DEFAULTTOPRIMARY,
    },
    System::LibraryLoader::GetModuleHandleW,
    UI::{
        Input::KeyboardAndMouse::{
            GetAsyncKeyState, GetCapture, ReleaseCapture, SetCapture, VK_LBUTTON,
        },
        WindowsAndMessaging::{
            CreateWindowExW, DefWindowProcW, DestroyWindow, GetCursorPos, GetSystemMetrics,
            IsWindowVisible, KillTimer, LoadCursorW, RegisterClassW, SetTimer, SetWindowPos,
            ShowWindow, UpdateLayeredWindow, IDC_ARROW, MA_NOACTIVATE, SM_CXDRAG, SM_CXSCREEN,
            SM_CYDRAG, SM_CYSCREEN, SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOZORDER, SW_HIDE,
            SW_SHOWNOACTIVATE, ULW_ALPHA, WM_CANCELMODE, WM_CAPTURECHANGED, WM_LBUTTONDOWN,
            WM_LBUTTONUP, WM_MOUSEACTIVATE, WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_NCDESTROY, WM_TIMER,
            WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_POPUP,
        },
    },
};

const EDGE: usize = 224;
const SOURCE_EDGE: usize = SIZE as usize;
const MIN_SIZE: i32 = 100;
const MAX_SIZE: i32 = 512;
const WHEEL_STEP: i32 = 120;
const DOT: usize = 20;
const BYTES: usize = SOURCE_EDGE * SOURCE_EDGE * 4;
const ERROR: &str = "revision_pet_window_failed";
const TICK: usize = 1;
const DRAG_TICK: usize = 2;
const BUBBLE_TICK: usize = 3;
const POSITION_FILE: &str = "pet-position.json";
const SNAP_MARGIN: i64 = 12;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
enum DockEdge {
    Left,
    Right,
    Top,
    Bottom,
}

impl DockEdge {
    fn near(point: POINT, work: RECT, size: i32) -> Option<Self> {
        [Self::Left, Self::Right, Self::Top, Self::Bottom]
            .into_iter()
            .min_by_key(|edge| edge.distance(point, work, size))
            .filter(|edge| edge.distance(point, work, size) <= SNAP_MARGIN)
    }

    fn distance(self, point: POINT, work: RECT, size: i32) -> i64 {
        match self {
            Self::Left => i64::from(point.x) - i64::from(work.left),
            Self::Right => i64::from(work.right) - i64::from(point.x) - i64::from(size),
            Self::Top => i64::from(point.y) - i64::from(work.top),
            Self::Bottom => i64::from(work.bottom) - i64::from(point.y) - i64::from(size),
        }
    }

    fn is_side(self) -> bool {
        matches!(self, Self::Left | Self::Right)
    }

    fn pin(self, position: &mut POINT, work: RECT, size: i32) {
        match self {
            Self::Left => position.x = work.left,
            Self::Right => position.x = work.right - size,
            Self::Top => position.y = work.top,
            Self::Bottom => position.y = work.bottom - size,
        }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(untagged)]
enum SavedPosition {
    Free(i32, i32),
    Docked(i32, i32, DockEdge),
    Scaled {
        x: i32,
        y: i32,
        size: i32,
        dock: Option<DockEdge>,
    },
}

fn work_area(point: POINT, primary: bool) -> RECT {
    let mut info = MONITORINFO {
        cbSize: std::mem::size_of::<MONITORINFO>() as u32,
        ..Default::default()
    };
    let flag = if primary {
        MONITOR_DEFAULTTOPRIMARY
    } else {
        MONITOR_DEFAULTTONEAREST
    };
    if unsafe { GetMonitorInfoW(MonitorFromPoint(point, flag), &mut info) } != 0 {
        info.rcWork
    } else {
        RECT {
            left: 0,
            top: 0,
            right: unsafe { GetSystemMetrics(SM_CXSCREEN) },
            bottom: unsafe { GetSystemMetrics(SM_CYSCREEN) },
        }
    }
}

fn clamp_to_work_area(point: POINT, bounds: RECT, edge: i32) -> POINT {
    POINT {
        x: point
            .x
            .clamp(bounds.left, (bounds.right - edge).max(bounds.left)),
        y: point
            .y
            .clamp(bounds.top, (bounds.bottom - edge).max(bounds.top)),
    }
}

fn cursor() -> Option<POINT> {
    let mut point = POINT::default();
    (unsafe { GetCursorPos(&mut point) } != 0).then_some(point)
}

fn pet_work_area(position: POINT, size: i32) -> RECT {
    work_area(
        POINT {
            x: position.x + size / 2,
            y: position.y + size / 2,
        },
        false,
    )
}

fn initial_position(root: &std::path::Path) -> (POINT, Option<DockEdge>, i32) {
    let saved = (|| {
        let mut bytes = Vec::new();
        fs::File::open(root.join(POSITION_FILE))
            .ok()?
            .take(128)
            .read_to_end(&mut bytes)
            .ok()?;
        let (x, y, dock, size) = match serde_json::from_slice(&bytes).ok()? {
            SavedPosition::Free(x, y) => (x, y, None, EDGE as i32),
            SavedPosition::Docked(x, y, edge) => (x, y, Some(edge), EDGE as i32),
            SavedPosition::Scaled { x, y, size, dock } => {
                (x, y, dock, size.clamp(MIN_SIZE, MAX_SIZE))
            }
        };
        let point = POINT { x, y };
        let work = work_area(point, false);
        let position = clamp_to_work_area(point, work, size);
        // Discard the saved edge if the monitor layout changed and it is no longer nearby.
        let dock = dock.filter(|edge| edge.distance(point, work, size).abs() <= SNAP_MARGIN);
        Some((position, dock, size))
    })();
    saved.unwrap_or_else(|| {
        let work = work_area(POINT::default(), true);
        let point = POINT {
            x: work.right - EDGE as i32 - 36,
            y: work.bottom - EDGE as i32 - 24,
        };
        (
            clamp_to_work_area(point, work, EDGE as i32),
            None,
            EDGE as i32,
        )
    })
}

fn dot_pixels() -> &'static [u8] {
    static PIXELS: OnceLock<Vec<u8>> = OnceLock::new();
    PIXELS.get_or_init(|| {
        let mut pixels = vec![0; BYTES];
        for y in 0..DOT {
            for x in 0..DOT {
                let (dx, dy) = (2 * x as i32 - 19, 2 * y as i32 - 19);
                if dx * dx + dy * dy <= 16 * 16 {
                    let at = (y * SOURCE_EDGE + x) * 4;
                    pixels[at..at + 4].copy_from_slice(&[128, 128, 128, 255]);
                }
            }
        }
        pixels
    })
}
thread_local! {
    static PET: RefCell<Option<Pet>> = const { RefCell::new(None) };
}

pub(crate) struct Sprite {
    key: String,
    cover: String,
    actions: Vec<String>,
    reaction_action: String,
    idle: Vec<Vec<u8>>,
    reaction: Vec<Vec<u8>>,
    dragging: Vec<Vec<u8>>,
    peek_left: Vec<Vec<u8>>,
    peek_right: Vec<Vec<u8>>,
    idle_bounds: RECT,
    left_bounds: RECT,
    right_bounds: RECT,
    head_height: i32,
    click_lines: Vec<String>,
    fps: u64,
}

fn mirror_track(track: &[Vec<u8>]) -> Vec<Vec<u8>> {
    track
        .iter()
        .map(|frame| {
            let mut mirror = Vec::with_capacity(BYTES);
            for row in frame.chunks_exact(SOURCE_EDGE * 4) {
                for pixel in row.chunks_exact(4).rev() {
                    mirror.extend_from_slice(pixel);
                }
            }
            mirror
        })
        .collect()
}

fn content_bounds(track: &[Vec<u8>]) -> Option<RECT> {
    let mut bounds = RECT {
        left: SOURCE_EDGE as i32,
        top: SOURCE_EDGE as i32,
        right: 0,
        bottom: 0,
    };
    for frame in track {
        for (index, pixel) in frame.chunks_exact(4).enumerate() {
            if pixel[3] >= 24 {
                let x = (index % SOURCE_EDGE) as i32;
                let y = (index / SOURCE_EDGE) as i32;
                bounds.left = bounds.left.min(x);
                bounds.top = bounds.top.min(y);
                bounds.right = bounds.right.max(x + 1);
                bounds.bottom = bounds.bottom.max(y + 1);
            }
        }
    }
    (bounds.left < bounds.right && bounds.top < bounds.bottom).then_some(bounds)
}

impl Sprite {
    pub(crate) fn from_frames(frames: Frames) -> Self {
        let cover = frames
            .idle
            .first()
            .and_then(|frame| {
                idle_thumbnail(frame, content_bounds(std::slice::from_ref(frame))?).ok()
            })
            .map(|png| format!("data:image/png;base64,{}", STANDARD.encode(png)))
            .unwrap_or_default();
        let convert = |frames: Vec<Vec<u8>>| -> Vec<Vec<u8>> {
            frames
                .into_iter()
                .map(|frame| layered_pixels(&frame))
                .collect()
        };
        let idle = convert(frames.idle);
        let mut peek_left = convert(frames.peek_left);
        let mut peek_right = convert(frames.peek_right);
        if peek_left.is_empty() {
            peek_left = mirror_track(&peek_right);
        } else if peek_right.is_empty() {
            peek_right = mirror_track(&peek_left);
        }
        let idle_bounds = content_bounds(&idle).unwrap_or(
            FrameView::full(POINT::default(), SOURCE_EDGE as i32, SOURCE_EDGE as i32).source,
        );
        let head_height =
            ((idle_bounds.bottom - idle_bounds.top) as f64 / frames.head_body_ratio).round() as i32;
        // Peek names describe the viewer's direction, so the artwork points into the desktop.
        let left_bounds = content_bounds(&peek_right).unwrap_or(idle_bounds);
        let right_bounds = content_bounds(&peek_left).unwrap_or(idle_bounds);
        Self {
            key: "catalog:whale".into(),
            cover,
            actions: frames.actions,
            reaction_action: frames.reaction_action,
            idle,
            reaction: convert(frames.reaction),
            dragging: convert(frames.dragging),
            peek_left,
            peek_right,
            idle_bounds,
            left_bounds,
            right_bounds,
            head_height: head_height.max(1),
            click_lines: frames.click_lines,
            fps: frames.fps,
        }
    }

    pub(crate) fn with_key(mut self, key: String) -> Self {
        self.key = key;
        self
    }
}

#[derive(Clone, Copy)]
struct FrameView {
    source: RECT,
    position: POINT,
    size: WinSize,
    flip_vertical: bool,
}

impl FrameView {
    fn full(position: POINT, size: i32, source_edge: i32) -> Self {
        Self {
            source: RECT {
                left: 0,
                top: 0,
                right: source_edge,
                bottom: source_edge,
            },
            position,
            size: WinSize { cx: size, cy: size },
            flip_vertical: false,
        }
    }

    fn at_edge(
        edge: DockEdge,
        position: POINT,
        work: RECT,
        content: RECT,
        head_height: i32,
        size: i32,
    ) -> Self {
        let scale = |pixels| (pixels * size + SOURCE_EDGE as i32 / 2) / SOURCE_EDGE as i32;
        let mut source = content;
        let mut position = POINT {
            x: position.x + scale(content.left),
            y: position.y + scale(content.top),
        };
        match edge {
            DockEdge::Top | DockEdge::Bottom => {
                source.bottom = (source.top + head_height).min(content.bottom);
                position.y = if edge == DockEdge::Top {
                    work.top
                } else {
                    work.bottom - scale(source.bottom - source.top).max(1)
                };
            }
            DockEdge::Left => {
                source.left = (content.left + content.right - 1) / 2;
                position.x = work.left;
            }
            DockEdge::Right => {
                source.right = (content.left + content.right + 1) / 2;
                position.x = work.right - scale(source.right - source.left).max(1);
            }
        }
        Self {
            source,
            position,
            size: WinSize {
                cx: scale(source.right - source.left).max(1),
                cy: scale(source.bottom - source.top).max(1),
            },
            flip_vertical: edge == DockEdge::Top,
        }
    }

    fn bounds(self) -> RECT {
        RECT {
            left: self.position.x,
            top: self.position.y,
            right: self.position.x + self.size.cx,
            bottom: self.position.y + self.size.cy,
        }
    }
}

struct Surface {
    dc: HDC,
    bitmap: HBITMAP,
    stock: HGDIOBJ,
    data: *mut c_void,
}
impl Surface {
    fn new() -> Result<Self, &'static str> {
        let mut info = BITMAPINFO::default();
        info.bmiHeader.biSize = std::mem::size_of_val(&info.bmiHeader) as u32;
        info.bmiHeader.biWidth = SOURCE_EDGE as i32;
        info.bmiHeader.biHeight = -(SOURCE_EDGE as i32); // Top-down rows, same as PNG.
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;
        let mut surface = Self {
            dc: ptr::null_mut(),
            bitmap: ptr::null_mut(),
            stock: ptr::null_mut(),
            data: ptr::null_mut(),
        };
        // A single memory DC owns the selected top-down DIB; Drop restores the stock bitmap.
        unsafe {
            surface.dc = CreateCompatibleDC(ptr::null_mut());
            if surface.dc.is_null() {
                return Err(ERROR);
            }
            surface.bitmap = CreateDIBSection(
                surface.dc,
                &info,
                DIB_RGB_COLORS,
                &mut surface.data,
                ptr::null_mut(),
                0,
            );
            if surface.bitmap.is_null() || surface.data.is_null() {
                return Err(ERROR);
            }
            surface.stock = SelectObject(surface.dc, surface.bitmap);
            if surface.stock.is_null() {
                return Err(ERROR);
            }
        }
        Ok(surface)
    }

    fn paint(&self, hwnd: HWND, pixels: &[u8], view: FrameView) -> bool {
        if pixels.len() != BYTES {
            return false;
        }
        let source_width = (view.source.right - view.source.left) as usize;
        let source_height = (view.source.bottom - view.source.top) as usize;
        let width = view.size.cx as usize;
        let height = view.size.cy as usize;
        let stride = SOURCE_EDGE * 4;
        let from = POINT { x: 0, y: 0 };
        let blend = BLENDFUNCTION {
            BlendOp: AC_SRC_OVER as u8,
            BlendFlags: 0,
            SourceConstantAlpha: 255,
            AlphaFormat: AC_SRC_ALPHA as u8,
        };
        // The DIB is the exact CPU-owned 32-bit row layout allocated in new().
        unsafe {
            GdiFlush();
            let dib = std::slice::from_raw_parts_mut(self.data.cast::<u8>(), BYTES);
            for (y, row) in dib.chunks_exact_mut(stride).take(height).enumerate() {
                let offset_y = y * source_height / height;
                let source_y = if view.flip_vertical {
                    view.source.bottom as usize - 1 - offset_y
                } else {
                    view.source.top as usize + offset_y
                };
                let start = source_y * stride + view.source.left as usize * 4;
                let source = &pixels[start..start + source_width * 4];
                if source_width == width {
                    row[..width * 4].copy_from_slice(source);
                } else {
                    for (x, pixel) in row[..width * 4].chunks_exact_mut(4).enumerate() {
                        let offset = (x * source_width / width) * 4;
                        pixel.copy_from_slice(&source[offset..offset + 4]);
                    }
                }
            }
            UpdateLayeredWindow(
                hwnd,
                ptr::null_mut(),
                &view.position,
                &view.size,
                self.dc,
                &from,
                0,
                &blend,
                ULW_ALPHA,
            ) != 0
        }
    }
}
impl Drop for Surface {
    fn drop(&mut self) {
        unsafe {
            if !self.stock.is_null() {
                SelectObject(self.dc, self.stock);
            }
            if !self.bitmap.is_null() {
                DeleteObject(self.bitmap);
            }
            if !self.dc.is_null() {
                DeleteDC(self.dc);
            }
        }
    }
}

struct Pet {
    window: HWND,
    surface: Surface,
    sprite: Sprite,
    rates: ActionRates,
    timer_interval: Option<u32>,
    frame: usize,
    reacting: bool,
    compact: bool,
    size: i32,
    wheel_delta: i32,
    position: POINT,
    dock: Option<DockEdge>,
    root: PathBuf,
    drag: Option<Drag>,
    bubble: Option<bubble::Bubble>,
    next_line: usize,
}

struct Drag {
    cursor: POINT,
    origin: POINT,
    moved: bool,
    dock: Option<DockEdge>,
}

impl Pet {
    fn action_track(&self) -> (&str, &[Vec<u8>]) {
        if self.dock == Some(DockEdge::Left) && !self.sprite.peek_right.is_empty() {
            ("peek_right", &self.sprite.peek_right)
        } else if self.dock == Some(DockEdge::Right) && !self.sprite.peek_left.is_empty() {
            ("peek_left", &self.sprite.peek_left)
        } else if self.drag.as_ref().is_some_and(|drag| drag.moved)
            && !self.sprite.dragging.is_empty()
        {
            ("dragging", &self.sprite.dragging)
        } else if self.reacting {
            (&self.sprite.reaction_action, &self.sprite.reaction)
        } else {
            ("idle", &self.sprite.idle)
        }
    }

    fn track(&self) -> &[Vec<u8>] {
        self.action_track().1
    }

    fn tick_interval(&self) -> u32 {
        let (action, _) = self.action_track();
        let fps = self
            .rates
            .get(action)
            .or_else(|| match action {
                "peek_left" if !self.sprite.actions.iter().any(|key| key == action) => {
                    self.rates.get("peek_right")
                }
                "peek_right" if !self.sprite.actions.iter().any(|key| key == action) => {
                    self.rates.get("peek_left")
                }
                _ => None,
            })
            .map_or(self.sprite.fps, |fps| u64::from(*fps));
        ((1000 + fps / 2) / fps) as u32
    }

    fn start_tick(&mut self) -> bool {
        let interval = self.tick_interval();
        if self.timer_interval == Some(interval) {
            return true;
        }
        if unsafe { SetTimer(self.window, TICK, interval, None) } == 0 {
            return false;
        }
        self.timer_interval = Some(interval);
        true
    }

    fn sync_visible_tick(&mut self) {
        if !self.compact && unsafe { IsWindowVisible(self.window) } != 0 {
            self.start_tick();
        }
    }

    fn stop_tick(&mut self) {
        unsafe { KillTimer(self.window, TICK) };
        self.timer_interval = None;
    }

    fn view(&self) -> FrameView {
        if self.compact {
            return FrameView::full(self.position, DOT as i32, DOT as i32);
        }
        let Some(edge) = self.dock else {
            return FrameView::full(self.position, self.size, SOURCE_EDGE as i32);
        };
        let content = match edge {
            DockEdge::Left => self.sprite.left_bounds,
            DockEdge::Right => self.sprite.right_bounds,
            DockEdge::Top | DockEdge::Bottom => self.sprite.idle_bounds,
        };
        FrameView::at_edge(
            edge,
            self.position,
            pet_work_area(self.position, self.size),
            content,
            self.sprite.head_height,
            self.size,
        )
    }

    fn paint(&self) -> bool {
        let pixels = if self.compact {
            dot_pixels()
        } else {
            let track = self.track();
            let frame = if self.dock.is_some_and(DockEdge::is_side) && self.frame >= track.len() {
                2 * track.len() - 2 - self.frame
            } else {
                self.frame
            };
            &track[frame]
        };
        self.surface.paint(self.window, pixels, self.view())
    }

    fn begin_press(&mut self) {
        let Some(cursor) = cursor() else { return };
        if self.drag.is_some() {
            return;
        }
        self.hide_bubble();
        self.drag = Some(Drag {
            cursor,
            origin: self.position,
            moved: false,
            dock: None,
        });
        unsafe {
            SetCapture(self.window);
            SetTimer(self.window, DRAG_TICK, 16, None);
        }
    }

    fn move_drag(&mut self) {
        let (Some(pointer), Some(drag)) = (cursor(), self.drag.as_mut()) else {
            return;
        };
        let dx = pointer.x - drag.cursor.x;
        let dy = pointer.y - drag.cursor.y;
        if !drag.moved {
            let (x, y) = unsafe { (GetSystemMetrics(SM_CXDRAG), GetSystemMetrics(SM_CYDRAG)) };
            if dx.abs() < x.max(2) && dy.abs() < y.max(2) {
                return;
            }
            drag.moved = true;
            self.frame = 0;
            self.reacting = false;
        }
        let undocking = self.dock.take().is_some();
        let edge = if self.compact { DOT as i32 } else { self.size };
        let desired = POINT {
            x: drag.origin.x + dx,
            y: drag.origin.y + dy,
        };
        let work = work_area(pointer, false);
        drag.dock = if self.compact {
            None
        } else {
            DockEdge::near(desired, work, self.size)
        };
        let position = clamp_to_work_area(desired, work, edge);
        if undocking {
            self.position = position;
            self.paint();
        } else if (self.position.x != position.x || self.position.y != position.y)
            && unsafe {
                SetWindowPos(
                    self.window,
                    ptr::null_mut(),
                    position.x,
                    position.y,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                )
            } != 0
        {
            self.position = position;
        }
        self.sync_visible_tick();
    }

    fn end_press(&mut self, complete: bool) {
        if complete {
            self.move_drag();
        }
        let Some(drag) = self.drag.take() else { return };
        unsafe {
            KillTimer(self.window, DRAG_TICK);
            if GetCapture() == self.window {
                ReleaseCapture();
            }
        }
        if drag.moved {
            if complete {
                self.dock = drag.dock;
            }
            self.save_position();
        } else if complete && !self.compact {
            self.reacting = !self.dock.is_some_and(DockEdge::is_side);
            self.show_line();
        }
        self.frame = 0;
        if !self.compact {
            self.paint();
            self.sync_visible_tick();
        }
    }

    fn save_position(&self) {
        let write = || -> std::io::Result<()> {
            fs::create_dir_all(&self.root)?;
            let mut file = tempfile::NamedTempFile::new_in(&self.root)?;
            let POINT { x, y } = self.position;
            let position = if self.size != EDGE as i32 {
                SavedPosition::Scaled {
                    x,
                    y,
                    size: self.size,
                    dock: self.dock,
                }
            } else {
                self.dock.map_or(SavedPosition::Free(x, y), |edge| {
                    SavedPosition::Docked(x, y, edge)
                })
            };
            serde_json::to_writer(&mut file, &position)?;
            file.flush()?;
            file.persist(self.root.join(POSITION_FILE))
                .map_err(|error| error.error)?;
            Ok(())
        };
        let _ = write();
    }

    fn zoom(&mut self, delta: i32) {
        if self.compact || self.drag.is_some() {
            return;
        }
        let Some(pointer) = cursor() else { return };
        self.wheel_delta += delta;
        let steps = self.wheel_delta / WHEEL_STEP;
        self.wheel_delta %= WHEEL_STEP;
        if steps == 0 {
            return;
        }
        let work = pet_work_area(self.position, self.size);
        let max = MAX_SIZE
            .min(work.right - work.left)
            .min(work.bottom - work.top)
            .max(MIN_SIZE);
        let factor: f64 = if steps > 0 { 1.1 } else { 0.9 };
        let size =
            ((self.size as f64 * factor.powi(steps.abs())).round() as i32).clamp(MIN_SIZE, max);
        if size == self.size {
            return;
        }
        let previous = (self.position, self.size);
        let mut desired = POINT {
            x: pointer.x - (pointer.x - self.position.x) * size / self.size,
            y: pointer.y - (pointer.y - self.position.y) * size / self.size,
        };
        if let Some(edge) = self.dock {
            edge.pin(&mut desired, work, size);
        }
        self.position = clamp_to_work_area(desired, work, size);
        self.size = size;
        if self.paint() {
            self.hide_bubble();
            self.save_position();
        } else {
            (self.position, self.size) = previous;
        }
    }

    fn show_line(&mut self) {
        let Some(line) = self.sprite.click_lines.get(self.next_line) else {
            return;
        };
        if self.bubble.is_none() {
            self.bubble = bubble::Bubble::new(self.window);
        }
        let Some(bubble) = &self.bubble else { return };
        if bubble.show(
            line,
            self.view().bounds(),
            pet_work_area(self.position, self.size),
        ) {
            self.next_line = (self.next_line + 1) % self.sprite.click_lines.len();
            unsafe {
                SetTimer(self.window, BUBBLE_TICK, 3500, None);
            }
        }
    }

    fn hide_bubble(&self) {
        unsafe {
            KillTimer(self.window, BUBBLE_TICK);
        }
        if let Some(bubble) = &self.bubble {
            bubble.hide();
        }
    }
}

unsafe extern "system" fn window_proc(hwnd: HWND, message: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    match message {
        WM_MOUSEACTIVATE => return MA_NOACTIVATE as LRESULT,
        WM_TIMER | WM_LBUTTONDOWN | WM_LBUTTONUP | WM_MOUSEMOVE | WM_MOUSEWHEEL
        | WM_CAPTURECHANGED | WM_CANCELMODE | WM_NCDESTROY => {
            let handled = PET
                .try_with(|slot| {
                    let Ok(mut slot) = slot.try_borrow_mut() else {
                        return false;
                    };
                    let Some(pet) = slot.as_mut().filter(|pet| pet.window == hwnd) else {
                        return false;
                    };
                    match message {
                        WM_TIMER if w == TICK && !pet.compact => {
                            pet.frame += 1;
                            if pet.reacting && pet.frame >= pet.sprite.reaction.len() {
                                pet.frame = 0;
                                pet.reacting = false;
                            } else {
                                let len = pet.track().len();
                                let cycle = if pet.dock.is_some_and(DockEdge::is_side) {
                                    (2 * len - 2).max(1)
                                } else {
                                    len
                                };
                                pet.frame %= cycle;
                            }
                            pet.paint();
                            pet.sync_visible_tick();
                        }
                        WM_TIMER if w == DRAG_TICK => {
                            if unsafe { GetAsyncKeyState(VK_LBUTTON as i32) } < 0 {
                                pet.move_drag();
                            } else {
                                pet.end_press(true);
                            }
                        }
                        WM_TIMER if w == BUBBLE_TICK => pet.hide_bubble(),
                        WM_LBUTTONDOWN => pet.begin_press(),
                        WM_LBUTTONUP => pet.end_press(true),
                        WM_MOUSEMOVE if pet.drag.is_some() => pet.move_drag(),
                        WM_MOUSEWHEEL => pet.zoom((w >> 16) as i16 as i32),
                        WM_CAPTURECHANGED if l as HWND != hwnd => pet.end_press(false),
                        WM_CANCELMODE => pet.end_press(false),
                        WM_NCDESTROY => {
                            unsafe {
                                KillTimer(hwnd, TICK);
                                KillTimer(hwnd, DRAG_TICK);
                                KillTimer(hwnd, BUBBLE_TICK);
                            };
                            slot.take();
                            return false;
                        }
                        _ => return false,
                    }
                    true
                })
                .unwrap_or(false);
            if handled {
                return 0;
            }
        }
        _ => {}
    }
    unsafe { DefWindowProcW(hwnd, message, w, l) }
}

pub(crate) fn show(
    sprite: Sprite,
    root: PathBuf,
    commit: impl FnOnce() -> Result<(), &'static str>,
) -> Result<(), &'static str> {
    let class: Vec<u16> = "MoeGuardNativePetWindow\0".encode_utf16().collect();
    let surface = Surface::new()?;
    let previous = PET.with(|slot| {
        slot.borrow()
            .as_ref()
            .map(|pet| (pet.window, pet.compact, pet.position, pet.dock, pet.size))
    });
    let (position, dock, size) = previous.map_or_else(
        || initial_position(&root),
        |(_, _, position, dock, size)| (position, dock, size),
    );
    // Created, drawn and destroyed on the same UI thread as the window procedure.
    unsafe {
        let instance = GetModuleHandleW(ptr::null());
        if instance.is_null() {
            return Err(ERROR);
        }
        let info = WNDCLASSW {
            lpfnWndProc: Some(window_proc),
            hInstance: instance,
            hCursor: LoadCursorW(ptr::null_mut(), IDC_ARROW),
            lpszClassName: class.as_ptr(),
            ..Default::default()
        };
        if RegisterClassW(&info) == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS {
            return Err(ERROR);
        }
        let window = CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
            class.as_ptr(),
            class.as_ptr(),
            WS_POPUP,
            position.x,
            position.y,
            size,
            size,
            ptr::null_mut(),
            ptr::null_mut(),
            instance,
            ptr::null(),
        );
        if window.is_null() {
            return Err(ERROR);
        }
        let visible = previous.is_none_or(|(old, _, _, _, _)| IsWindowVisible(old) != 0);
        let compact = previous.is_some_and(|(_, compact, _, _, _)| compact);
        let rates = pet_fps::load(&root, &sprite.key);
        let mut pet = Pet {
            window,
            surface,
            sprite,
            rates,
            timer_interval: None,
            frame: 0,
            reacting: false,
            compact,
            size,
            wheel_delta: 0,
            position,
            dock,
            root,
            drag: None,
            bubble: None,
            next_line: 0,
        };
        if !pet.paint() || (visible && !compact && !pet.start_tick()) {
            dispose(pet);
            return Err(ERROR);
        }
        if let Err(error) = commit() {
            dispose(pet);
            return Err(error);
        }
        let previous = PET.with(|slot| slot.replace(Some(pet)));
        if visible {
            ShowWindow(window, SW_SHOWNOACTIVATE);
        }
        if let Some(old) = previous {
            dispose(old);
        }
    }
    Ok(())
}

pub(crate) fn toggle_visibility() {
    let hidden = PET.with(|slot| {
        let mut slot = slot.borrow_mut();
        let Some(pet) = slot.as_mut() else {
            return false;
        };
        pet.end_press(false);
        pet.hide_bubble();
        let window = pet.window;
        unsafe {
            if IsWindowVisible(window) != 0 {
                ShowWindow(window, SW_HIDE);
                pet.stop_tick();
                true
            } else if pet.compact || pet.start_tick() {
                ShowWindow(window, SW_SHOWNOACTIVATE);
                false
            } else {
                false
            }
        }
    });
    #[cfg(feature = "patrol-worker")]
    if hidden {
        crate::patrol_win32::stop();
    }
    #[cfg(not(feature = "patrol-worker"))]
    let _ = hidden;
}

pub(crate) fn appearance() -> serde_json::Value {
    PET.with(|slot| {
        let slot = slot.borrow();
        slot.as_ref().map_or(serde_json::Value::Null, |pet| {
            serde_json::json!({
                "key":pet.sprite.key, "cover":pet.sprite.cover, "actions":pet.sprite.actions,
                "defaultFps":pet.sprite.fps, "rates":pet.rates,
            })
        })
    })
}

pub(crate) fn set_action_fps(role: &str, action: &str, fps: u8) -> Result<(), &'static str> {
    PET.with(|slot| {
        let mut slot = slot.borrow_mut();
        let pet = slot.as_mut().ok_or("pet_fps_invalid")?;
        if role != pet.sprite.key || !pet.sprite.actions.iter().any(|key| key == action) {
            return Err("pet_fps_invalid");
        }
        pet_fps::save(&pet.root, role, action, fps)?;
        pet.rates.insert(action.to_owned(), fps);
        pet.sync_visible_tick();
        Ok(())
    })
}

pub(crate) fn toggle_compact() {
    #[cfg(feature = "patrol-worker")]
    crate::patrol_win32::stop();
    change_compact(None);
}

#[cfg(feature = "patrol-worker")]
pub(crate) fn patrol_allowed() -> bool {
    PET.with(|slot| {
        slot.borrow()
            .as_ref()
            .is_some_and(|pet| !pet.compact && unsafe { IsWindowVisible(pet.window) } != 0)
    })
}

pub(crate) fn exit_compact() {
    change_compact(Some(false));
}

fn change_compact(requested: Option<bool>) {
    let _ = PET.try_with(|slot| {
        let Ok(mut slot) = slot.try_borrow_mut() else {
            return;
        };
        let Some(pet) = slot.as_mut() else {
            return;
        };
        if requested == Some(pet.compact) {
            return;
        }
        pet.end_press(false);
        pet.hide_bubble();
        if pet.compact {
            let visible = unsafe { IsWindowVisible(pet.window) } != 0;
            let old_position = pet.position;
            pet.position =
                clamp_to_work_area(pet.position, work_area(pet.position, false), pet.size);
            pet.frame = 0;
            pet.reacting = false;
            pet.compact = false;
            if (visible && !pet.start_tick()) || !pet.paint() {
                pet.compact = true;
                pet.position = old_position;
                pet.stop_tick();
            }
        } else {
            pet.compact = true;
            if pet.paint() {
                pet.stop_tick();
            } else {
                pet.compact = false;
            }
        }
    });
}

fn dispose(mut pet: Pet) {
    drop(pet.bubble.take());
    unsafe {
        if GetCapture() == pet.window {
            ReleaseCapture();
        }
        KillTimer(pet.window, TICK);
        KillTimer(pet.window, DRAG_TICK);
        KillTimer(pet.window, BUBBLE_TICK);
        DestroyWindow(pet.window);
    }
}

#[cfg(test)]
#[test]
fn settings_idle_thumbnail_crops_the_visible_frame() {
    for (left, top, width, height, expected) in
        [(185, 95, 62, 184, (66, 188)), (35, 185, 420, 80, (212, 42))]
    {
        let mut frame = vec![0; BYTES];
        frame[3] = 8;
        for y in top..top + height {
            for x in left..left + width {
                let start = (y * SOURCE_EDGE + x) * 4;
                frame[start..start + 4].copy_from_slice(&[40, 130, 220, 255]);
            }
        }
        let bounds = content_bounds(std::slice::from_ref(&frame)).unwrap();
        let png = idle_thumbnail(&frame, bounds).unwrap();
        let mut reader = png::Decoder::new(std::io::Cursor::new(png))
            .read_info()
            .unwrap();
        assert_eq!((reader.info().width, reader.info().height), expected);
        let mut pixels = vec![0; reader.output_buffer_size().unwrap()];
        reader.next_frame(&mut pixels).unwrap();
        assert_eq!(pixels[3], 0);
        assert_eq!(
            &pixels[pixels.len() / 2 + (expected.0 as usize / 2) * 4..][..4],
            &[40, 130, 220, 255]
        );
    }
}

#[cfg(test)]
#[test]
fn playback_interval_uses_the_rate_of_the_active_action() {
    let root = tempfile::tempdir().unwrap();
    let mut pet = Pet {
        window: ptr::null_mut(),
        surface: Surface::new().unwrap(),
        sprite: Sprite::from_frames(crate::role_catalog::bundled_pet().unwrap()),
        rates: ActionRates::from([("idle".into(), 1), ("click_reaction".into(), 60)]),
        timer_interval: None,
        frame: 0,
        reacting: false,
        compact: false,
        size: EDGE as i32,
        wheel_delta: 0,
        position: POINT::default(),
        dock: None,
        root: root.path().to_owned(),
        drag: None,
        bubble: None,
        next_line: 0,
    };
    assert_eq!(pet.tick_interval(), 1000);
    pet.reacting = true;
    assert_eq!(pet.action_track().0, "click_reaction");
    assert_eq!(pet.tick_interval(), 17);
    pet.reacting = false;
    assert_eq!(pet.tick_interval(), 1000);
}

pub(crate) fn is_active() -> bool {
    PET.with(|slot| slot.borrow().is_some())
}

pub(crate) fn close() {
    if let Some(pet) = PET.with(|slot| slot.take()) {
        dispose(pet);
    }
}

#[cfg(test)]
#[test]
fn desktop_edge_views_crop_the_role_and_face_inward() {
    let sprite = Sprite::from_frames(crate::role_catalog::bundled_pet().unwrap());
    assert!(sprite.peek_left.len() > 1);
    assert_eq!(sprite.peek_left.len(), sprite.peek_right.len());
    assert_eq!(sprite.peek_left, mirror_track(&sprite.peek_right));
    let work = RECT {
        left: -1280,
        top: -120,
        right: 0,
        bottom: 900,
    };
    let size = EDGE as i32;
    let positions = [
        (
            DockEdge::Left,
            POINT {
                x: work.left,
                y: 250,
            },
        ),
        (
            DockEdge::Right,
            POINT {
                x: work.right - size,
                y: 250,
            },
        ),
        (
            DockEdge::Top,
            POINT {
                x: -750,
                y: work.top,
            },
        ),
        (
            DockEdge::Bottom,
            POINT {
                x: -750,
                y: work.bottom - size,
            },
        ),
    ];
    for (edge, position) in positions {
        assert_eq!(DockEdge::near(position, work, size), Some(edge));
        let content = match edge {
            DockEdge::Left => sprite.left_bounds,
            DockEdge::Right => sprite.right_bounds,
            _ => sprite.idle_bounds,
        };
        let view = FrameView::at_edge(edge, position, work, content, sprite.head_height, size);
        let bounds = view.bounds();
        assert!(bounds.left >= work.left && bounds.right <= work.right);
        assert!(bounds.top >= work.top && bounds.bottom <= work.bottom);
        match edge {
            DockEdge::Left => {
                assert_eq!(bounds.left, work.left);
                assert!(view.source.left > content.left);
            }
            DockEdge::Right => {
                assert_eq!(bounds.right, work.right);
                assert!(view.source.right < content.right);
            }
            DockEdge::Top | DockEdge::Bottom => {
                assert_eq!(view.source.top, content.top);
                assert_eq!(view.source.bottom - view.source.top, sprite.head_height);
                assert!(bounds.bottom - bounds.top < size);
                assert!(view.source.bottom < content.bottom);
                assert_eq!(view.flip_vertical, edge == DockEdge::Top);
                assert!(bounds.top == work.top || bounds.bottom == work.bottom);
            }
        }
    }
    assert_eq!(DockEdge::near(POINT { x: -750, y: 250 }, work, size), None);
}
