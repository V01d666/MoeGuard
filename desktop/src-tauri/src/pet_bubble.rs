//! The click line is drawn in its own non-activating window above the pet.
use std::ptr;
use windows_sys::Win32::{
    Foundation::{
        GetLastError, ERROR_CLASS_ALREADY_EXISTS, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM,
    },
    Graphics::Gdi::{
        BeginPaint, CombineRgn, CreateFontW, CreatePolygonRgn, CreateRoundRectRgn,
        CreateSolidBrush, DeleteObject, DrawTextW, EndPaint, FillRect, FrameRgn, GetDC,
        InvalidateRect, ReleaseDC, SelectObject, SetBkMode, SetTextColor, SetWindowRgn, ALTERNATE,
        DT_CALCRECT, DT_CENTER, DT_NOPREFIX, DT_SINGLELINE, DT_WORDBREAK, HRGN, PAINTSTRUCT,
        RGN_OR, TRANSPARENT,
    },
    System::LibraryLoader::GetModuleHandleW,
    UI::{
        HiDpi::GetDpiForWindow,
        WindowsAndMessaging::{
            CreateWindowExW, DefWindowProcW, DestroyWindow, GetWindow, GetWindowLongPtrW,
            GetWindowTextW, RegisterClassW, SetWindowLongPtrW, SetWindowPos, SetWindowTextW,
            ShowWindow, GWLP_USERDATA, GW_OWNER, HTTRANSPARENT, HWND_TOPMOST, MA_NOACTIVATE,
            SWP_NOACTIVATE, SWP_SHOWWINDOW, SW_HIDE, WM_MOUSEACTIVATE, WM_NCHITTEST, WM_PAINT,
            WNDCLASSW, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_POPUP,
        },
    },
};

const TEXT: u32 = DT_CENTER | DT_NOPREFIX | DT_WORDBREAK;
const TOP_TIP: isize = 1;
const BOTTOM_TIP: isize = 2;

pub(super) struct Bubble {
    window: HWND,
    owner: HWND,
}

fn scaled(owner: HWND, units: i32) -> i32 {
    let dpi = unsafe { GetDpiForWindow(owner) }.clamp(96, 144) as i32;
    (units * dpi + 48) / 96
}

fn bubble_shape(mut body: RECT, owner: HWND, style: isize) -> (RECT, HRGN) {
    let tip = style & 3;
    let anchor = (style >> 2) as i32;
    let depth = scaled(owner, 7);
    let half_base = scaled(owner, 6);
    let radius = scaled(owner, 14);
    if tip == TOP_TIP {
        body.top += depth;
    } else if tip == BOTTOM_TIP {
        body.bottom -= depth;
    }
    unsafe {
        let region =
            CreateRoundRectRgn(body.left, body.top, body.right, body.bottom, radius, radius);
        if !region.is_null() && tip != 0 {
            let (base, point) = if tip == TOP_TIP {
                (body.top + 2, body.top - depth)
            } else {
                (body.bottom - 2, body.bottom + depth)
            };
            let points = [
                POINT {
                    x: anchor - half_base,
                    y: base,
                },
                POINT {
                    x: anchor,
                    y: point,
                },
                POINT {
                    x: anchor + half_base,
                    y: base,
                },
            ];
            let triangle = CreatePolygonRgn(points.as_ptr(), points.len() as i32, ALTERNATE);
            if !triangle.is_null() {
                CombineRgn(region, region, triangle, RGN_OR);
                DeleteObject(triangle);
            }
        }
        (body, region)
    }
}

fn placement(pet: RECT, width: i32, height: i32, gap: i32, work: RECT) -> POINT {
    let center_x = pet.left + (pet.right - pet.left - width) / 2;
    let center_y = pet.top + (pet.bottom - pet.top - height) / 2;
    let above = pet.top - gap - height;
    let below = pet.bottom + gap;
    let right = pet.right + gap;
    let left = pet.left - gap - width;
    let (x, y) = if above >= work.top {
        (center_x, above)
    } else if below + height <= work.bottom {
        (center_x, below)
    } else if right + width <= work.right {
        (right, center_y)
    } else if left >= work.left {
        (left, center_y)
    } else if pet.top - work.top >= work.bottom - pet.bottom {
        (center_x, above)
    } else {
        (center_x, below)
    };
    POINT {
        x: x.clamp(work.left, work.right - width),
        y: y.clamp(work.top, work.bottom - height),
    }
}

#[cfg(test)]
#[test]
fn click_bubble_uses_the_available_space_at_each_edge() {
    let work = RECT {
        left: -1280,
        top: -120,
        right: 0,
        bottom: 620,
    };
    let width = 500;
    let height = 154;
    let edge = 224;
    let gap = 12;
    for pet in [
        POINT {
            x: work.left,
            y: work.top,
        },
        POINT {
            x: work.right - edge,
            y: work.top,
        },
        POINT {
            x: work.left,
            y: work.bottom - edge,
        },
        POINT {
            x: work.right - edge,
            y: work.bottom - edge,
        },
    ] {
        let bounds = RECT {
            left: pet.x,
            top: pet.y,
            right: pet.x + edge,
            bottom: pet.y + edge,
        };
        let bubble = placement(bounds, width, height, gap, work);
        assert!(bubble.x >= work.left && bubble.x + width <= work.right);
        assert!(bubble.y >= work.top && bubble.y + height <= work.bottom);
        assert!(bubble.y + height + gap <= pet.y || bubble.y >= pet.y + edge + gap);
    }
}

impl Bubble {
    pub(super) fn new(owner: HWND) -> Option<Self> {
        let class: Vec<u16> = "MoeGuardSpeechBubble".encode_utf16().chain([0]).collect();
        unsafe {
            let instance = GetModuleHandleW(ptr::null());
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
                WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
                class.as_ptr(),
                ptr::null(),
                WS_POPUP,
                0,
                0,
                0,
                0,
                owner,
                ptr::null_mut(),
                instance,
                ptr::null(),
            );
            (!window.is_null()).then_some(Self { window, owner })
        }
    }

    pub(super) fn show(&self, line: &str, pet: RECT, work: RECT) -> bool {
        let text: Vec<u16> = line.encode_utf16().chain([0]).collect();
        let horizontal = scaled(self.owner, 12);
        let vertical = scaled(self.owner, 9);
        let max_width = scaled(self.owner, 252).min((work.right - work.left).max(1));
        let min_width = scaled(self.owner, 92).min(max_width);
        unsafe {
            let dc = GetDC(self.window);
            if dc.is_null() {
                return false;
            }
            let font = bubble_font(self.owner);
            let stock = SelectObject(dc, font);
            let mut single_line = RECT::default();
            DrawTextW(
                dc,
                text.as_ptr(),
                -1,
                &mut single_line,
                DT_CALCRECT | DT_SINGLELINE | DT_NOPREFIX,
            );
            let width = (single_line.right + horizontal * 2).clamp(min_width, max_width);
            let mut measure = RECT {
                left: 0,
                top: 0,
                right: (width - 2 * horizontal).max(1),
                bottom: 0,
            };
            let text_height = DrawTextW(dc, text.as_ptr(), -1, &mut measure, TEXT | DT_CALCRECT);
            if !stock.is_null() {
                SelectObject(dc, stock);
            }
            if !font.is_null() {
                DeleteObject(font);
            }
            ReleaseDC(self.window, dc);
            if text_height <= 0 {
                return false;
            }
            let depth = scaled(self.owner, 7);
            let height = (text_height + 2 * vertical + depth).min((work.bottom - work.top).max(1));
            let point = placement(pet, width, height, scaled(self.owner, 4), work);
            let tip = if point.y + height <= pet.top {
                BOTTOM_TIP
            } else if point.y >= pet.bottom {
                TOP_TIP
            } else {
                0
            };
            let inset = scaled(self.owner, 19).min(width / 2);
            let anchor =
                (pet.left + (pet.right - pet.left) / 2 - point.x).clamp(inset, width - inset);
            let style = ((anchor as isize) << 2) | tip;
            SetWindowLongPtrW(self.window, GWLP_USERDATA, style);
            SetWindowTextW(self.window, text.as_ptr());
            let layout = RECT {
                left: 0,
                top: 0,
                right: width,
                bottom: height,
            };
            let (_, region) = bubble_shape(layout, self.owner, style);
            if !region.is_null() && SetWindowRgn(self.window, region, 1) == 0 {
                DeleteObject(region);
            }
            let shown = SetWindowPos(
                self.window,
                HWND_TOPMOST,
                point.x,
                point.y,
                width,
                height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            ) != 0;
            InvalidateRect(self.window, ptr::null(), 1);
            shown
        }
    }

    pub(super) fn hide(&self) {
        unsafe {
            ShowWindow(self.window, SW_HIDE);
        }
    }
}

impl Drop for Bubble {
    fn drop(&mut self) {
        unsafe {
            DestroyWindow(self.window);
        }
    }
}

unsafe fn bubble_font(owner: HWND) -> windows_sys::Win32::Graphics::Gdi::HFONT {
    let name: Vec<u16> = "Microsoft YaHei UI".encode_utf16().chain([0]).collect();
    unsafe {
        CreateFontW(
            -scaled(owner, 16),
            0,
            0,
            0,
            400,
            0,
            0,
            0,
            1,
            0,
            0,
            4,
            0,
            name.as_ptr(),
        )
    }
}

unsafe extern "system" fn window_proc(hwnd: HWND, message: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    match message {
        WM_NCHITTEST => HTTRANSPARENT as LRESULT,
        WM_MOUSEACTIVATE => MA_NOACTIVATE as LRESULT,
        WM_PAINT => unsafe {
            let mut paint = PAINTSTRUCT::default();
            let dc = BeginPaint(hwnd, &mut paint);
            if !dc.is_null() {
                let mut bounds = RECT::default();
                windows_sys::Win32::UI::WindowsAndMessaging::GetClientRect(hwnd, &mut bounds);
                let owner = GetWindow(hwnd, GW_OWNER);
                let style = GetWindowLongPtrW(hwnd, GWLP_USERDATA);
                let (mut text_bounds, region) = bubble_shape(bounds, owner, style);
                let background = CreateSolidBrush(0x00fffbf7);
                FillRect(dc, &bounds, background);
                DeleteObject(background);
                if !region.is_null() {
                    let outline = CreateSolidBrush(0x00eac89c);
                    let thickness = scaled(owner, 1);
                    FrameRgn(dc, region, outline, thickness, thickness);
                    DeleteObject(outline);
                    DeleteObject(region);
                }
                let mut text = [0u16; 241];
                let count = GetWindowTextW(hwnd, text.as_mut_ptr(), text.len() as i32);
                let font = bubble_font(owner);
                let horizontal = scaled(owner, 12);
                let stock = SelectObject(dc, font);
                SetTextColor(dc, 0x00654723);
                SetBkMode(dc, TRANSPARENT as i32);
                text_bounds.left += horizontal;
                text_bounds.right -= horizontal;
                text_bounds.top += scaled(owner, 9);
                DrawTextW(dc, text.as_ptr(), count, &mut text_bounds, TEXT);
                if !stock.is_null() {
                    SelectObject(dc, stock);
                }
                if !font.is_null() {
                    DeleteObject(font);
                }
                EndPaint(hwnd, &paint);
            }
            0
        },
        _ => unsafe { DefWindowProcW(hwnd, message, w, l) },
    }
}
