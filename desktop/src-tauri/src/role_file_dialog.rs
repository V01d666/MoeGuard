//! Only settings may ask the OS for a role source or export destination.
use std::{ffi::OsString, os::windows::ffi::OsStringExt, path::PathBuf};
use windows_sys::Win32::{
    Foundation::HWND,
    UI::Controls::Dialogs::{
        CommDlgExtendedError, GetOpenFileNameW, GetSaveFileNameW, OFN_DONTADDTORECENT,
        OFN_EXPLORER, OFN_FILEMUSTEXIST, OFN_NOCHANGEDIR, OFN_PATHMUSTEXIST, OPENFILENAMEW,
    },
};

pub(crate) fn choose(owner: HWND, save: bool) -> Result<Option<PathBuf>, &'static str> {
    let export_filter = "MoeGuard role (*.moeguard-role)\0*.moeguard-role\0\0";
    let import_filter =
        "MoeGuard role (*.moeguard-role, *.zip, role.json)\0*.moeguard-role;*.zip;role.json\0\0";
    let filter: Vec<u16> = if save { export_filter } else { import_filter }
        .encode_utf16()
        .collect();
    let extension: Vec<u16> = "moeguard-role\0".encode_utf16().collect();
    let mut text = vec![0u16; 32768];
    if save {
        for (out, value) in text.iter_mut().zip("MoeGuard.moeguard-role".encode_utf16()) {
            *out = value;
        }
    }
    let mut dialog = OPENFILENAMEW {
        lStructSize: std::mem::size_of::<OPENFILENAMEW>() as u32,
        hwndOwner: owner,
        lpstrFilter: filter.as_ptr(),
        nFilterIndex: 1,
        lpstrFile: text.as_mut_ptr(),
        nMaxFile: text.len() as u32,
        lpstrDefExt: if save {
            extension.as_ptr()
        } else {
            std::ptr::null()
        },
        Flags: OFN_EXPLORER
            | OFN_NOCHANGEDIR
            | OFN_DONTADDTORECENT
            | OFN_PATHMUSTEXIST
            | if save { 0 } else { OFN_FILEMUSTEXIST },
        ..Default::default()
    };
    let result = unsafe {
        if save {
            GetSaveFileNameW(&mut dialog)
        } else {
            GetOpenFileNameW(&mut dialog)
        }
    };
    if result == 0 {
        return if unsafe { CommDlgExtendedError() } == 0 {
            Ok(None)
        } else {
            Err("role_transfer_dialog")
        };
    }
    let end = text
        .iter()
        .position(|value| *value == 0)
        .ok_or("role_transfer_dialog")?;
    Ok(Some(PathBuf::from(OsString::from_wide(&text[..end]))))
}
