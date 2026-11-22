//! Keep the mailbox available after the workshop WebView closes.
use crate::{
    mail_reads::{MailReads, TrayInbox},
    native_tray,
    role_catalog::revision,
};
use serde_json::Value;
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager};

#[derive(Default)]
pub(crate) struct TrayMail {
    inbox: Mutex<TrayInbox>,
    open: AtomicBool,
}

pub(crate) fn begin(app: AppHandle, root: PathBuf, home: Option<PathBuf>) {
    app.manage(TrayMail::default());
    tauri::async_runtime::spawn(async move {
        loop {
            let (public, private) = tokio::join!(
                revision::announcements(),
                revision::background_mail(root.clone(), home.clone()),
            );
            let host = app.clone();
            let notices = tauri::async_runtime::spawn_blocking(move || {
                #[cfg(all(windows, feature = "legacy-workshop"))]
                let mut fresh = TrayInbox::default();
                if let Ok(rows) = public {
                    update(&host, &rows, true);
                    #[cfg(all(windows, feature = "legacy-workshop"))]
                    fresh.replace(&rows, true);
                }
                let private = private.unwrap_or(Value::Null);
                update(&host, &private, false);
                #[cfg(all(windows, feature = "legacy-workshop"))]
                {
                    fresh.replace(&private, false);
                    host.state::<MailReads>()
                        .claim_notifications(&fresh)
                        .unwrap_or(0)
                }
            })
            .await;
            #[cfg(all(windows, feature = "legacy-workshop"))]
            if let Ok(count) = notices {
                if count > 0 && show_notice(&app, count).await {
                    open(app.clone()).await;
                }
            }
            #[cfg(not(all(windows, feature = "legacy-workshop")))]
            let _ = notices;
            tokio::time::sleep(Duration::from_secs(6 * 60 * 60)).await;
        }
    });
}

#[cfg(all(windows, feature = "legacy-workshop"))]
async fn show_notice(app: &AppHandle, count: usize) -> bool {
    let copy = app
        .state::<Mutex<crate::ui_locale::UiLocale>>()
        .lock()
        .ok()
        .map(|language| {
            (
                language.message("mail.title"),
                language
                    .message("mail.notification")
                    .replace("{n}", &count.to_string()),
            )
        });
    let Some((title, text)) = copy else {
        return false;
    };
    tauri::async_runtime::spawn_blocking(move || {
        crate::mail_balloon_win32::show(title, text).unwrap_or(false)
    })
    .await
    .unwrap_or(false)
}

pub(crate) fn update(app: &AppHandle, reply: &Value, public: bool) {
    let state = app.state::<TrayMail>();
    if let Ok(mut inbox) = state.inbox.lock() {
        inbox.replace(reply, public);
        refresh(app, &inbox);
    };
}

pub(crate) fn marked(app: &AppHandle) {
    let state = app.state::<TrayMail>();
    if let Ok(inbox) = state.inbox.lock() {
        refresh(app, &inbox);
    };
}

fn refresh(app: &AppHandle, inbox: &TrayInbox) {
    native_tray::set_mail_unread(app, app.state::<MailReads>().tray_unread(inbox));
}

pub(crate) fn take_open(app: &AppHandle) -> bool {
    app.state::<TrayMail>().open.swap(false, Ordering::AcqRel)
}

pub(crate) async fn open(app: AppHandle) {
    app.state::<TrayMail>().open.store(true, Ordering::Release);
    if crate::open_workshop(app.clone()).await.is_ok() {
        if let Some(window) = app.get_webview_window("workshop") {
            let _ = window.emit("tray-mailbox", ());
        }
    }
}
