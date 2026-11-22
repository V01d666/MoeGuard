use base64::{engine::general_purpose::STANDARD, Engine};
use std::ptr::null;
use windows::Win32::{
    Graphics::Imaging::{
        CLSID_WICImagingFactory, GUID_WICPixelFormat32bppRGBA, IWICImagingFactory,
        WICBitmapDitherTypeNone, WICBitmapInterpolationModeFant, WICBitmapPaletteTypeCustom,
        WICDecodeMetadataCacheOnDemand,
    },
    System::Com::{
        CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER,
        COINIT_MULTITHREADED,
    },
};

const ERROR: &str = "picture_decode_failed";
const MAX_INPUT: usize = 64 * 1024 * 1024;
const EDGE: u32 = 2048;

struct Apartment;

impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { CoUninitialize() };
    }
}

fn from_windows_codec(bytes: &[u8]) -> Result<(u32, u32, Vec<u8>), &'static str> {
    // WIC keeps borrowing the input until all COM objects in this scope are dropped.
    unsafe {
        CoInitializeEx(None, COINIT_MULTITHREADED)
            .ok()
            .map_err(|_| ERROR)?;
        let _apartment = Apartment;
        let factory: IWICImagingFactory =
            CoCreateInstance(&CLSID_WICImagingFactory, None, CLSCTX_INPROC_SERVER)
                .map_err(|_| ERROR)?;
        let stream = factory.CreateStream().map_err(|_| ERROR)?;
        stream.InitializeFromMemory(bytes).map_err(|_| ERROR)?;
        let decoder = factory
            .CreateDecoderFromStream(&stream, null(), WICDecodeMetadataCacheOnDemand)
            .map_err(|_| ERROR)?;
        let frame = decoder.GetFrame(0).map_err(|_| ERROR)?;
        let (mut width, mut height) = (0, 0);
        frame.GetSize(&mut width, &mut height).map_err(|_| ERROR)?;
        if width == 0 || height == 0 {
            return Err(ERROR);
        }
        let longest = width.max(height);
        let converter = factory.CreateFormatConverter().map_err(|_| ERROR)?;
        if longest > EDGE {
            width = (u64::from(width) * u64::from(EDGE) / u64::from(longest)).max(1) as u32;
            height = (u64::from(height) * u64::from(EDGE) / u64::from(longest)).max(1) as u32;
            let scaler = factory.CreateBitmapScaler().map_err(|_| ERROR)?;
            scaler
                .Initialize(&frame, width, height, WICBitmapInterpolationModeFant)
                .map_err(|_| ERROR)?;
            converter
                .Initialize(
                    &scaler,
                    &GUID_WICPixelFormat32bppRGBA,
                    WICBitmapDitherTypeNone,
                    None,
                    0.0,
                    WICBitmapPaletteTypeCustom,
                )
                .map_err(|_| ERROR)?;
        } else {
            converter
                .Initialize(
                    &frame,
                    &GUID_WICPixelFormat32bppRGBA,
                    WICBitmapDitherTypeNone,
                    None,
                    0.0,
                    WICBitmapPaletteTypeCustom,
                )
                .map_err(|_| ERROR)?;
        }
        let stride = width * 4;
        let mut rgba = vec![0; (stride * height) as usize];
        converter
            .CopyPixels(null(), stride, &mut rgba)
            .map_err(|_| ERROR)?;
        Ok((width, height, rgba))
    }
}

pub(crate) fn decode(input: String) -> Result<String, &'static str> {
    if input.len() > MAX_INPUT.div_ceil(3) * 4 {
        return Err(ERROR);
    }
    let bytes = STANDARD.decode(input).map_err(|_| ERROR)?;
    if bytes.is_empty() || bytes.len() > MAX_INPUT {
        return Err(ERROR);
    }
    let (width, height, rgba) = from_windows_codec(&bytes)?;
    let mut output = Vec::new();
    let mut encoder = png::Encoder::new(&mut output, width, height);
    encoder.set_color(png::ColorType::Rgba);
    let mut writer = encoder.write_header().map_err(|_| ERROR)?;
    writer.write_image_data(&rgba).map_err(|_| ERROR)?;
    writer.finish().map_err(|_| ERROR)?;
    Ok(format!("data:image/png;base64,{}", STANDARD.encode(output)))
}

#[cfg(test)]
#[test]
fn windows_image_import_decodes_a_bitmap_without_using_its_extension() {
    let mut bmp = [0; 58];
    bmp[..2].copy_from_slice(b"BM");
    bmp[2] = 58;
    bmp[10] = 54;
    bmp[14] = 40;
    bmp[18] = 1;
    bmp[22] = 1;
    bmp[26] = 1;
    bmp[28] = 24;
    bmp[54..57].copy_from_slice(&[9, 7, 5]);
    let (width, height, rgba) = from_windows_codec(&bmp).unwrap();
    assert_eq!((width, height), (1, 1));
    assert_eq!(rgba, [5, 7, 9, 255]);
}
