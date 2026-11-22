//! Native camera indicator, visible before starting the capture worker.
use std::{cell::RefCell, ptr::null_mut};
use windows_sys::Win32::{
    Foundation::{
        GetLastError, ERROR_CLASS_ALREADY_EXISTS, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM,
    },
    Graphics::Gdi::{
        BeginPaint, CreateEllipticRgn, CreateFontW, CreatePolygonRgn, CreateRoundRectRgn,
        CreateSolidBrush, DeleteObject, DrawTextW, EndPaint, FillRect, FillRgn, FrameRgn,
        InvalidateRect, SelectObject, SetBkMode, SetTextColor, SetWindowRgn, UpdateWindow,
        ALTERNATE, DT_END_ELLIPSIS, DT_NOPREFIX, DT_SINGLELINE, DT_VCENTER, HDC, PAINTSTRUCT,
        TRANSPARENT,
    },
    System::LibraryLoader::GetModuleHandleW,
    UI::{
        HiDpi::{GetDpiForSystem, GetDpiForWindow},
        WindowsAndMessaging::{
            CreateWindowExW, DefWindowProcW, DestroyWindow, GetClientRect, GetSystemMetrics,
            GetWindowTextW, IsWindowVisible, RegisterClassW, SetWindowPos, SetWindowTextW,
            ShowWindow, SystemParametersInfoW, HTTRANSPARENT, MA_NOACTIVATE, SM_CXSCREEN,
            SM_CYSCREEN, SPI_GETWORKAREA, SWP_NOACTIVATE, SWP_NOZORDER, SW_SHOWNOACTIVATE,
            WM_DISPLAYCHANGE, WM_DPICHANGED, WM_MOUSEACTIVATE, WM_NCHITTEST, WM_PAINT,
            WM_SETTINGCHANGE, WNDCLASSW, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
            WS_POPUP,
        },
    },
};

thread_local! {
    static LABELS: RefCell<[String; 2]> = const { RefCell::new([String::new(), String::new()]) };
}

pub(super) fn set_labels(labels: [String; 2]) {
    LABELS.replace(labels);
}

pub(super) struct Indicator {
    window: HWND,
    preview: bool,
}

fn title(preview: bool) -> Vec<u16> {
    LABELS.with(|labels| {
        let labels = labels.borrow();
        labels[usize::from(preview)]
            .encode_utf16()
            .chain([0])
            .collect()
    })
}

fn scale(dpi: u32, units: i32) -> i32 {
    (units * dpi.max(96) as i32 + 48) / 96
}

fn layout() -> RECT {
    let dpi = unsafe { GetDpiForSystem() };
    let mut area = RECT::default();
    if unsafe { SystemParametersInfoW(SPI_GETWORKAREA, 0, (&mut area as *mut RECT).cast(), 0) } == 0
    {
        area.right = unsafe { GetSystemMetrics(SM_CXSCREEN) };
        area.bottom = unsafe { GetSystemMetrics(SM_CYSCREEN) };
    }
    let width = scale(dpi, 220).min((area.right - area.left).max(1));
    let height = scale(dpi, 38).min((area.bottom - area.top).max(1));
    let margin = scale(dpi, 14);
    let x = (area.right - margin - width).max(area.left);
    let y = (area.top + margin).min(area.bottom - height);
    RECT {
        left: x,
        top: y,
        right: x + width,
        bottom: y + height,
    }
}

fn position(hwnd: HWND) -> bool {
    let bounds = layout();
    let width = bounds.right - bounds.left;
    let height = bounds.bottom - bounds.top;
    unsafe {
        let corner = scale(GetDpiForSystem(), 22);
        let region = CreateRoundRectRgn(0, 0, width + 1, height + 1, corner, corner);
        if !region.is_null() && SetWindowRgn(hwnd, region, 1) == 0 {
            DeleteObject(region);
        }
        SetWindowPos(
            hwnd,
            null_mut(),
            bounds.left,
            bounds.top,
            width,
            height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        ) != 0
    }
}

impl Indicator {
    pub(super) fn show(preview: bool) -> Option<Self> {
        let class: Vec<u16> = "MoeGuardCameraIndicator"
            .encode_utf16()
            .chain([0])
            .collect();
        let text = title(preview);
        unsafe {
            let instance = GetModuleHandleW(std::ptr::null());
            let info = WNDCLASSW {
                lpfnWndProc: Some(window_proc),
                hInstance: instance,
                lpszClassName: class.as_ptr(),
                ..Default::default()
            };
            if RegisterClassW(&info) == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS {
                return None;
            }
            let window = CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                class.as_ptr(),
                text.as_ptr(),
                WS_POPUP,
                0,
                0,
                0,
                0,
                null_mut(),
                null_mut(),
                instance,
                std::ptr::null(),
            );
            if window.is_null() {
                return None;
            }
            let indicator = Self { window, preview };
            if !position(window) {
                return None;
            }
            ShowWindow(window, SW_SHOWNOACTIVATE);
            UpdateWindow(window);
            indicator.is_visible().then_some(indicator)
        }
    }

    pub(super) fn is_visible(&self) -> bool {
        unsafe { IsWindowVisible(self.window) != 0 }
    }

    pub(super) fn refresh_title(&self) {
        unsafe {
            SetWindowTextW(self.window, title(self.preview).as_ptr());
            InvalidateRect(self.window, std::ptr::null(), 0);
        }
    }
}

impl Drop for Indicator {
    fn drop(&mut self) {
        unsafe { DestroyWindow(self.window) };
    }
}

const fn color(red: u32, green: u32, blue: u32) -> u32 {
    red | (green << 8) | (blue << 16)
}

fn paint(hwnd: HWND, dc: HDC) {
    unsafe {
        let mut bounds = RECT::default();
        GetClientRect(hwnd, &mut bounds);
        let dpi = GetDpiForWindow(hwnd);
        let px = |units| scale(dpi, units);
        let background = CreateSolidBrush(color(84, 21, 37));
        FillRect(dc, &bounds, background);
        DeleteObject(background);

        let outline = CreateSolidBrush(color(151, 62, 84));
        let outer = CreateRoundRectRgn(0, 0, bounds.right + 1, bounds.bottom + 1, px(22), px(22));
        FrameRgn(dc, outer, outline, px(1), px(1));
        DeleteObject(outer);
        DeleteObject(outline);

        let icon_plate = CreateRoundRectRgn(px(7), px(6), px(35), px(32), px(13), px(13));
        let plate_brush = CreateSolidBrush(color(155, 48, 74));
        FillRgn(dc, icon_plate, plate_brush);
        DeleteObject(icon_plate);
        DeleteObject(plate_brush);

        let light = color(255, 243, 246);
        let camera_brush = CreateSolidBrush(light);
        let camera = CreateRoundRectRgn(px(13), px(14), px(24), px(24), px(4), px(4));
        let lens_points = [
            POINT {
                x: px(23),
                y: px(17),
            },
            POINT {
                x: px(29),
                y: px(14),
            },
            POINT {
                x: px(29),
                y: px(24),
            },
            POINT {
                x: px(23),
                y: px(21),
            },
        ];
        let lens = CreatePolygonRgn(lens_points.as_ptr(), lens_points.len() as i32, ALTERNATE);
        FillRgn(dc, camera, camera_brush);
        FillRgn(dc, lens, camera_brush);
        DeleteObject(camera);
        DeleteObject(lens);
        DeleteObject(camera_brush);

        let dot = CreateEllipticRgn(bounds.right - px(19), px(15), bounds.right - px(11), px(23));
        let dot_brush = CreateSolidBrush(color(255, 113, 136));
        FillRgn(dc, dot, dot_brush);
        DeleteObject(dot);
        DeleteObject(dot_brush);

        let font_name: Vec<u16> = "Microsoft YaHei UI".encode_utf16().chain([0]).collect();
        let font = CreateFontW(
            -px(14),
            0,
            0,
            0,
            600,
            0,
            0,
            0,
            1,
            0,
            0,
            4,
            0,
            font_name.as_ptr(),
        );
        let previous = SelectObject(dc, font);
        let mut text = [0u16; 64];
        let count = GetWindowTextW(hwnd, text.as_mut_ptr(), text.len() as i32);
        let mut text_bounds = RECT {
            left: px(43),
            right: bounds.right - px(27),
            ..bounds
        };
        SetBkMode(dc, TRANSPARENT as i32);
        SetTextColor(dc, light);
        DrawTextW(
            dc,
            text.as_ptr(),
            count,
            &mut text_bounds,
            DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS,
        );
        if !previous.is_null() {
            SelectObject(dc, previous);
        }
        DeleteObject(font);
    }
}

unsafe extern "system" fn window_proc(hwnd: HWND, message: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    match message {
        WM_NCHITTEST => HTTRANSPARENT as LRESULT,
        WM_MOUSEACTIVATE => MA_NOACTIVATE as LRESULT,
        WM_DISPLAYCHANGE | WM_DPICHANGED | WM_SETTINGCHANGE => {
            position(hwnd);
            0
        }
        WM_PAINT => unsafe {
            let mut state = PAINTSTRUCT::default();
            let dc = BeginPaint(hwnd, &mut state);
            if !dc.is_null() {
                paint(hwnd, dc);
            }
            EndPaint(hwnd, &state);
            0
        },
        _ => unsafe { DefWindowProcW(hwnd, message, w, l) },
    }
}
