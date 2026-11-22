use std::{io::Cursor, mem::size_of, ptr::null_mut, slice};
use windows_sys::Win32::{
    Graphics::Gdi::{
        CreateDIBSection, DeleteObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS,
    },
    UI::{
        HiDpi::GetDpiForSystem,
        WindowsAndMessaging::{SetMenuItemInfoW, HMENU, MENUITEMINFOW, MIIM_BITMAP},
    },
};

const ICON_COUNT: usize = 6;
const ROW_DIP: u32 = 36;
const CELL: usize = 72;
const STRIDE: usize = ICON_COUNT * CELL * 4;

// The menu borrows these bitmaps for the lifetime of the tray.
pub(super) struct MenuBitmap(usize);

impl Drop for MenuBitmap {
    fn drop(&mut self) {
        unsafe { DeleteObject(self.0 as _) };
    }
}

fn read_atlas() -> Option<Vec<u8>> {
    let mut reader = png::Decoder::new(Cursor::new(include_bytes!("../tray-menu.png")))
        .read_info()
        .ok()?;
    let mut pixels = vec![0; CELL * STRIDE];
    let frame = reader.next_frame(&mut pixels).ok()?;
    (frame.width == (ICON_COUNT * CELL) as u32
        && frame.height == CELL as u32
        && frame.color_type == png::ColorType::Rgba
        && frame.bit_depth == png::BitDepth::Eight)
        .then_some(pixels)
}

fn icon_bitmap(atlas: &[u8], index: usize, size: usize) -> Option<MenuBitmap> {
    let info = BITMAPINFO {
        bmiHeader: BITMAPINFOHEADER {
            biSize: size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: size as i32,
            biHeight: -(size as i32),
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB,
            ..Default::default()
        },
        ..Default::default()
    };
    let mut bits = null_mut();
    let handle =
        unsafe { CreateDIBSection(null_mut(), &info, DIB_RGB_COLORS, &mut bits, null_mut(), 0) };
    if handle.is_null() {
        return None;
    }
    let bitmap = MenuBitmap(handle as usize);
    if bits.is_null() {
        return None;
    }
    // The top-down 32-bit DIB has no extra row padding.
    let bitmap_pixels = unsafe { slice::from_raw_parts_mut(bits.cast::<u8>(), size * size * 4) };
    for (offset, pixel) in bitmap_pixels.chunks_exact_mut(4).enumerate() {
        let x = (2 * (offset % size) + 1) * CELL / (2 * size);
        let y = (2 * (offset / size) + 1) * CELL / (2 * size);
        let source = y * STRIDE + (index * CELL + x) * 4;
        let alpha = atlas[source + 3];
        let premultiply = |channel| ((u16::from(channel) * u16::from(alpha) + 127) / 255) as u8;
        pixel.copy_from_slice(&[
            premultiply(atlas[source + 2]),
            premultiply(atlas[source + 1]),
            premultiply(atlas[source]),
            alpha,
        ]);
    }
    Some(bitmap)
}

pub(super) fn install(
    menu: HMENU,
    positions: impl IntoIterator<Item = (u32, usize)>,
) -> Vec<MenuBitmap> {
    let Some(atlas) = read_atlas() else {
        return Vec::new();
    };
    let dpi = unsafe { GetDpiForSystem() }.clamp(96, 480);
    let size = ((ROW_DIP * dpi + 48) / 96) as usize;
    positions
        .into_iter()
        .filter_map(|(position, index)| {
            let bitmap = icon_bitmap(&atlas, index, size)?;
            // The bitmap reserves touch-friendly space. Windows still draws
            // the text, selection, separators and keyboard focus.
            let item = MENUITEMINFOW {
                cbSize: size_of::<MENUITEMINFOW>() as u32,
                fMask: MIIM_BITMAP,
                hbmpItem: bitmap.0 as _,
                ..Default::default()
            };
            (unsafe { SetMenuItemInfoW(menu, position, 1, &item) } != 0).then_some(bitmap)
        })
        .collect()
}
