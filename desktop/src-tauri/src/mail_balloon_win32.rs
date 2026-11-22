//! Use the Shell notification API, not a private tray icon ID or a WebView.
use std::{
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};
use windows::{
    core::{implement, Error, Result, PCWSTR},
    Win32::{
        Foundation::{POINT, S_FALSE},
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER,
            COINIT_APARTMENTTHREADED,
        },
        UI::Shell::{
            IQueryContinue, IQueryContinue_Impl, IUserNotification2, IUserNotificationCallback,
            IUserNotificationCallback_Impl, UserNotification, NIIF_INFO, NIIF_NOSOUND,
            NIIF_RESPECT_QUIET_TIME,
        },
    },
};

#[implement(IQueryContinue, IUserNotificationCallback)]
struct Balloon {
    clicked: AtomicBool,
    until: Instant,
}

impl IQueryContinue_Impl for Balloon_Impl {
    fn QueryContinue(&self) -> Result<()> {
        if self.clicked.load(Ordering::Relaxed) || Instant::now() >= self.until {
            Err(Error::from_hresult(S_FALSE))
        } else {
            Ok(())
        }
    }
}

impl IUserNotificationCallback_Impl for Balloon_Impl {
    fn OnBalloonUserClick(&self, _: *const POINT) -> Result<()> {
        self.clicked.store(true, Ordering::Relaxed);
        Ok(())
    }
    fn OnLeftClick(&self, _: *const POINT) -> Result<()> {
        Ok(())
    }
    fn OnContextMenu(&self, _: *const POINT) -> Result<()> {
        Ok(())
    }
}

struct ComApartment;
impl Drop for ComApartment {
    fn drop(&mut self) {
        unsafe { CoUninitialize() }
    }
}

/// Blocking; call off the desktop event loop. Return true only for a balloon click.
pub(crate) fn show(title: String, text: String) -> Result<bool> {
    use windows::core::{ComObject, Interface};
    unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()? };
    let _apartment = ComApartment;
    let notice: IUserNotification2 =
        unsafe { CoCreateInstance(&UserNotification, None, CLSCTX_INPROC_SERVER)? };
    let title: Vec<_> = title.encode_utf16().chain([0]).collect();
    let text: Vec<_> = text.encode_utf16().chain([0]).collect();
    let balloon = ComObject::new(Balloon {
        clicked: AtomicBool::new(false),
        until: Instant::now() + Duration::from_secs(12),
    });
    let continue_when: IQueryContinue = balloon.to_interface();
    let click: IUserNotificationCallback = continue_when.cast()?;
    unsafe {
        notice.SetBalloonInfo(
            PCWSTR(title.as_ptr()),
            PCWSTR(text.as_ptr()),
            (NIIF_INFO | NIIF_NOSOUND | NIIF_RESPECT_QUIET_TIME).0,
        )?;
        notice.SetBalloonRetry(8000, 0, 0)?;
    }
    let shown = unsafe { notice.Show(&continue_when, 250, &click) };
    // The shell can report cancellation as we stop polling after a click.
    if balloon.clicked.load(Ordering::Relaxed) {
        Ok(true)
    } else {
        shown.map(|_| false)
    }
}
