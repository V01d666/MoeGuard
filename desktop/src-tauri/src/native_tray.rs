//! Opt-in native host entry points, with no WebView needed during startup.
#[cfg(windows)]
#[path = "tray_menu_win32.rs"]
mod tray_menu;

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
#[cfg(windows)]
use tauri::menu::ContextMenu;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Manager, RunEvent, WindowEvent, Wry,
};

use crate::ui_locale::UiLocale;

const WORKSHOP: &str = "tray-workshop";
const SETTINGS: &str = "tray-settings";
const MAILBOX: &str = "tray-mailbox";
const QUIT: &str = "tray-quit";
#[cfg(all(windows, feature = "patrol-worker"))]
const PATROL_START: &str = "tray-patrol-start";
#[cfg(all(windows, feature = "patrol-worker"))]
const PATROL_STOP: &str = "tray-patrol-stop";

#[cfg(any(test, all(windows, feature = "pet-win32")))]
fn toggles_pet(event: &tauri::tray::TrayIconEvent) -> bool {
    matches!(
        event,
        tauri::tray::TrayIconEvent::DoubleClick {
            button: tauri::tray::MouseButton::Left,
            ..
        }
    )
}

struct NativeTray {
    workshop: MenuItem<Wry>,
    settings: MenuItem<Wry>,
    mailbox: Option<MenuItem<Wry>>,
    unread: AtomicUsize,
    quit: MenuItem<Wry>,
    quitting: AtomicBool,
    #[cfg(all(windows, feature = "patrol-worker"))]
    patrol: Option<(MenuItem<Wry>, MenuItem<Wry>)>,
    #[cfg(windows)]
    _menu_bitmaps: Vec<tray_menu::MenuBitmap>,
}

pub(crate) fn install(app: &AppHandle, language: &UiLocale) -> tauri::Result<()> {
    let tray = NativeTray {
        workshop: MenuItem::with_id(
            app,
            WORKSHOP,
            language.message("settings.workshop"),
            true,
            None::<&str>,
        )?,
        settings: MenuItem::with_id(
            app,
            SETTINGS,
            language.message("tray.settings"),
            true,
            None::<&str>,
        )?,
        quit: MenuItem::with_id(app, QUIT, language.message("tray.quit"), true, None::<&str>)?,
        quitting: AtomicBool::new(false),
        mailbox: if cfg!(any(
            feature = "revision-loopback",
            all(windows, feature = "legacy-workshop")
        )) {
            Some(MenuItem::with_id(
                app,
                MAILBOX,
                mail_text(language, 0),
                true,
                None::<&str>,
            )?)
        } else {
            None
        },
        unread: AtomicUsize::new(0),
        #[cfg(all(windows, feature = "patrol-worker"))]
        patrol: if crate::patrol_win32::available() {
            Some((
                MenuItem::with_id(
                    app,
                    PATROL_START,
                    language.message("tray.patrolStart"),
                    true,
                    None::<&str>,
                )?,
                MenuItem::with_id(
                    app,
                    PATROL_STOP,
                    language.message("tray.patrolStop"),
                    true,
                    None::<&str>,
                )?,
            ))
        } else {
            None
        },
        #[cfg(windows)]
        _menu_bitmaps: Vec::new(),
    };
    let menu = Menu::with_items(app, &[&tray.workshop, &tray.settings])?;
    if let Some(mailbox) = &tray.mailbox {
        menu.append(mailbox)?;
    }
    #[cfg(all(windows, feature = "patrol-worker"))]
    if let Some((start, stop)) = &tray.patrol {
        menu.append_items(&[&PredefinedMenuItem::separator(app)?, start, stop])?;
    }
    menu.append_items(&[&PredefinedMenuItem::separator(app)?, &tray.quit])?;
    #[cfg(windows)]
    let tray = {
        let items = menu.items()?;
        let positions = items.iter().enumerate().filter_map(|(position, entry)| {
            let index = match entry.id().as_ref() {
                WORKSHOP => 0,
                SETTINGS => 1,
                MAILBOX => 2,
                #[cfg(feature = "patrol-worker")]
                PATROL_START => 3,
                #[cfg(feature = "patrol-worker")]
                PATROL_STOP => 4,
                QUIT => 5,
                _ => return None,
            };
            Some((position as u32, index))
        });
        let bitmaps = tray_menu::install(menu.hpopupmenu()? as _, positions);
        NativeTray {
            _menu_bitmaps: bitmaps,
            ..tray
        }
    };
    let icon = app
        .default_window_icon()
        .expect("bundled MoeGuard icon")
        .clone();
    let builder = TrayIconBuilder::with_id("moeguard");
    #[cfg(all(windows, feature = "pet-win32"))]
    let builder = builder
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if toggles_pet(&event) {
                let _ = tray
                    .app_handle()
                    .run_on_main_thread(crate::pet_win32::toggle_visibility);
            }
        });
    builder
        .icon(icon)
        .tooltip("萌卫 MoeGuard")
        .menu(&menu)
        .on_menu_event(|app, event| {
            let app = app.clone();
            match event.id().as_ref() {
                WORKSHOP => {
                    cancel_quit(&app);
                    tauri::async_runtime::spawn(async move {
                        let _ = crate::open_workshop(app).await;
                    });
                }
                SETTINGS => {
                    cancel_quit(&app);
                    tauri::async_runtime::spawn(async move {
                        let _ = crate::open_settings(app.clone(), app.state()).await;
                    });
                }
                #[cfg(any(
                    feature = "revision-loopback",
                    all(windows, feature = "legacy-workshop")
                ))]
                MAILBOX => {
                    cancel_quit(&app);
                    tauri::async_runtime::spawn(crate::tray_mail::open(app));
                }
                QUIT => {
                    tauri::async_runtime::spawn(quit(app));
                }
                #[cfg(all(windows, feature = "patrol-worker"))]
                PATROL_START => {
                    let settings_app = app.clone();
                    let _ = app.run_on_main_thread(move || {
                        crate::patrol_win32::start_with_setup(
                            false,
                            Some(Box::new(move || {
                                tauri::async_runtime::spawn(async move {
                                    let _ = crate::open_settings(
                                        settings_app.clone(),
                                        settings_app.state(),
                                    )
                                    .await;
                                });
                            })),
                        );
                    });
                }
                #[cfg(all(windows, feature = "patrol-worker"))]
                PATROL_STOP => {
                    let _ = app.run_on_main_thread(crate::patrol_win32::stop);
                }
                _ => {}
            }
        })
        .build(app)?;
    app.manage(tray);
    Ok(())
}

pub(crate) fn update_language(app: &AppHandle, language: &UiLocale) {
    let tray = app.state::<NativeTray>();
    let _ = tray
        .workshop
        .set_text(language.message("settings.workshop"));
    let _ = tray.settings.set_text(language.message("tray.settings"));
    let _ = tray.quit.set_text(language.message("tray.quit"));
    #[cfg(all(windows, feature = "patrol-worker"))]
    if let Some((start, stop)) = &tray.patrol {
        let _ = start.set_text(language.message("tray.patrolStart"));
        let _ = stop.set_text(language.message("tray.patrolStop"));
    }
    if let Some(mailbox) = &tray.mailbox {
        let _ = mailbox.set_text(mail_text(language, tray.unread.load(Ordering::Acquire)));
    }
}

fn mail_text(language: &UiLocale, count: usize) -> String {
    if count == 0 {
        language.message("mail.title")
    } else {
        format!("{} ({count})", language.message("mail.title"))
    }
}

#[cfg(any(
    feature = "revision-loopback",
    all(windows, feature = "legacy-workshop")
))]
pub(crate) fn set_mail_unread(app: &AppHandle, count: usize) {
    let tray = app.state::<NativeTray>();
    let language = app.state::<std::sync::Mutex<UiLocale>>();
    if let (Some(mailbox), Ok(language)) = (&tray.mailbox, language.lock()) {
        tray.unread.store(count, Ordering::Release);
        let _ = mailbox.set_text(mail_text(&language, count));
    };
}

pub(crate) fn cancel_quit(app: &AppHandle) {
    app.state::<NativeTray>()
        .quitting
        .store(false, Ordering::Release);
}

async fn quit(app: AppHandle) {
    let opening = app.state::<crate::WorkshopWindow>();
    let _guard = opening.0.lock().await;
    if let Some(window) = app.get_webview_window("workshop") {
        let tray = app.state::<NativeTray>();
        if !tray.quitting.swap(true, Ordering::AcqRel) && window.close().is_err() {
            cancel_quit(&app);
        }
    } else {
        app.exit(0);
    }
}

pub(crate) fn handle_run_event(app: &AppHandle, event: RunEvent) {
    match event {
        RunEvent::ExitRequested {
            code: None, api, ..
        } => api.prevent_exit(),
        RunEvent::WindowEvent {
            label,
            event: WindowEvent::Destroyed,
            ..
        } if label == "workshop" => {
            if app
                .state::<NativeTray>()
                .quitting
                .swap(false, Ordering::AcqRel)
            {
                app.exit(0);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::toggles_pet;
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};

    #[test]
    fn only_left_double_click_toggles_the_companion() {
        for (button, expected) in [(MouseButton::Left, true), (MouseButton::Right, false)] {
            assert_eq!(
                toggles_pet(&TrayIconEvent::DoubleClick {
                    id: "moeguard".into(),
                    position: (0.0, 0.0).into(),
                    rect: tauri::Rect::default(),
                    button,
                }),
                expected
            );
            assert!(!toggles_pet(&TrayIconEvent::Click {
                id: "moeguard".into(),
                position: (0.0, 0.0).into(),
                rect: tauri::Rect::default(),
                button,
                button_state: MouseButtonState::Up,
            }));
        }
    }
}
