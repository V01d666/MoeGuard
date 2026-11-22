#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(any(test, all(windows, feature = "legacy-workshop")))]
mod instance_guard;

#[cfg(all(windows, feature = "patrol-worker"))]
mod patrol_win32;
#[cfg(any(test, all(windows, feature = "pet-win32")))]
mod pet_hotkey;

#[cfg(any(
    all(test, feature = "revision-loopback"),
    all(windows, feature = "legacy-workshop")
))]
mod legacy_session;
#[cfg(all(windows, feature = "native-tray", feature = "legacy-workshop"))]
mod mail_balloon_win32;
#[cfg(any(
    feature = "revision-loopback",
    all(windows, feature = "legacy-workshop")
))]
mod mail_reads;
#[cfg(feature = "native-tray")]
mod native_tray;
#[cfg(all(windows, feature = "pet-win32"))]
mod pet_fps;
#[cfg(any(
    all(windows, feature = "pet-win32"),
    all(target_os = "linux", feature = "pet-gtk-probe")
))]
mod pet_frames;
#[cfg(all(feature = "pet-gtk-probe", target_os = "linux"))]
mod pet_probe;
#[cfg(all(windows, feature = "pet-win32"))]
mod pet_win32;
#[cfg(all(windows, feature = "legacy-workshop"))]
mod picture_import;
mod role_catalog;
#[cfg(all(windows, feature = "legacy-workshop", feature = "pet-win32"))]
mod role_file_dialog;
mod system_folder;
#[cfg(all(
    feature = "native-tray",
    any(
        feature = "revision-loopback",
        all(windows, feature = "legacy-workshop")
    )
))]
mod tray_mail;
mod ui_locale;
#[cfg(all(windows, feature = "windows-update"))]
mod windows_update;
mod workshop_store;

use role_catalog::{Catalog, Role};
use serde_json::Value;
use std::sync::{Arc, Mutex};
use tauri::{ipc::Channel, AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};
use ui_locale::UiLocale;
use workshop_store::WorkshopStore;

fn store(app: &AppHandle) -> Result<WorkshopStore, &'static str> {
    app.path()
        .app_local_data_dir()
        .map(WorkshopStore::new)
        .map_err(|_| "storage_unavailable")
}

#[cfg(all(
    windows,
    feature = "pet-win32",
    any(feature = "legacy-workshop", feature = "revision-loopback")
))]
fn removed_workshop_roles(state: &Value) -> Vec<String> {
    state["removedRoles"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

#[tauri::command]
async fn load_workshop(app: AppHandle) -> Result<Option<Value>, &'static str> {
    let store = store(&app)?;
    tauri::async_runtime::spawn_blocking(move || store.load().map_err(|_| "storage_read_failed"))
        .await
        .map_err(|_| "storage_unavailable")?
}

#[tauri::command]
async fn save_workshop(app: AppHandle, expected: u32, state: Value) -> Result<u32, &'static str> {
    let store = store(&app)?;
    #[cfg(all(windows, feature = "pet-win32", feature = "legacy-workshop"))]
    let removed = removed_workshop_roles(&state);
    let result = tauri::async_runtime::spawn_blocking(move || {
        store
            .save(expected, state)
            .map_err(|error| match error.kind() {
                std::io::ErrorKind::AlreadyExists => "storage_conflict",
                _ => "storage_write_failed",
            })
    })
    .await
    .unwrap_or(Err("storage_unavailable"));
    #[cfg(feature = "native-tray")]
    if result.is_err() {
        native_tray::cancel_quit(&app);
    }
    #[cfg(all(windows, feature = "pet-win32", feature = "legacy-workshop"))]
    if result.is_ok() && !removed.is_empty() {
        if let Ok(root) = app.path().app_local_data_dir() {
            let (done, completed) = tokio::sync::oneshot::channel();
            if app
                .run_on_main_thread(move || {
                    if role_catalog::revision::clear_removed_pet(
                        &root.join("revision-legacy"),
                        &removed,
                    )
                    .unwrap_or(false)
                    {
                        #[cfg(feature = "patrol-worker")]
                        patrol_win32::stop();
                        pet_win32::close();
                    }
                    let _ = done.send(());
                })
                .is_ok()
            {
                let _ = completed.await;
                let _ = app.emit_to("settings", "pet-playback-changed", ());
            }
        }
    }
    result
}

#[tauri::command]
async fn catalog_inventory(catalog: State<'_, Arc<Catalog>>) -> Result<Vec<Role>, &'static str> {
    let catalog = catalog.inner().clone();
    tauri::async_runtime::spawn_blocking(move || catalog.inventory())
        .await
        .map_err(|_| "catalog_storage")?
}

#[derive(serde::Deserialize)]
struct InstalledActionSource {
    remote_task_id: String,
    result_sha256: String,
}

#[tauri::command]
async fn workshop_action_preview(
    app: AppHandle,
    catalog: State<'_, Arc<Catalog>>,
    id: String,
    action: String,
    installed: Option<InstalledActionSource>,
) -> Result<String, &'static str> {
    let installed = installed.map(|source| (source.remote_task_id, source.result_sha256));
    #[cfg(all(windows, feature = "pet-win32", feature = "legacy-workshop"))]
    {
        use base64::Engine;
        let root = app
            .path()
            .app_local_data_dir()
            .map_err(|_| "storage_unavailable")?;
        let catalog = catalog.inner().clone();
        tauri::async_runtime::spawn_blocking(move || {
            let key = if installed.is_some() {
                role_catalog::revision::workshop_key(&id)
            } else {
                format!("catalog:{id}")
            };
            let rate = pet_fps::load(&root, &key).get(&action).copied();
            let png = match installed {
                Some((remote, digest)) => role_catalog::revision::installed_action_preview(
                    &root.join("revision-legacy"),
                    &remote,
                    &digest,
                    &id,
                    &action,
                    rate,
                ),
                None => catalog.action_preview(&id, &action, rate),
            }?;
            Ok(format!(
                "data:image/png;base64,{}",
                base64::engine::general_purpose::STANDARD.encode(png)
            ))
        })
        .await
        .map_err(|_| "catalog_preview_unavailable")?
    }
    #[cfg(not(all(windows, feature = "pet-win32", feature = "legacy-workshop")))]
    {
        let _ = (app, catalog, id, action, installed);
        Err("catalog_preview_unavailable")
    }
}

#[tauri::command]
async fn catalog_download(
    catalog: State<'_, Arc<Catalog>>,
    id: String,
    progress: Channel<u8>,
) -> Result<Role, &'static str> {
    catalog
        .inner()
        .download(&id, |value| {
            let _ = progress.send(value);
        })
        .await
}

#[tauri::command]
fn catalog_cancel(catalog: State<'_, Arc<Catalog>>, id: String) -> bool {
    catalog.cancel(&id)
}

#[tauri::command]
async fn workshop_revision(app: AppHandle, request: Value) -> Result<Value, &'static str> {
    #[cfg(all(
        feature = "native-tray",
        any(
            feature = "revision-loopback",
            all(windows, feature = "legacy-workshop")
        )
    ))]
    if request["op"] == "take_tray_mailbox" {
        return Ok(Value::Bool(tray_mail::take_open(&app)));
    }
    if request["op"] == "mode" {
        let uses_service = cfg!(any(
            feature = "revision-loopback",
            all(windows, feature = "legacy-workshop")
        ));
        return Ok(Value::from(if uses_service {
            "service"
        } else {
            "preview"
        }));
    }
    if matches!(
        request["op"].as_str(),
        Some("pet_library" | "enable_installed_pet" | "enable_catalog_pet")
    ) {
        #[cfg(all(
            any(
                feature = "revision-loopback",
                all(windows, feature = "legacy-workshop")
            ),
            any(
                all(windows, feature = "pet-win32"),
                all(target_os = "linux", feature = "pet-gtk-probe")
            )
        ))]
        if let Some(bridge) = app.try_state::<role_catalog::revision::RevisionWindow>() {
            let catalog = app.state::<Arc<Catalog>>().inner().clone();
            return bridge.pet_library(&app, catalog, request).await;
        }
        return if request["op"] == "pet_library" {
            Ok(Value::Null)
        } else {
            Err("revision_disabled")
        };
    }
    #[cfg(any(
        feature = "revision-loopback",
        all(windows, feature = "legacy-workshop")
    ))]
    if matches!(
        request["op"].as_str(),
        Some("announcements" | "mailbox" | "mail_mark")
    ) {
        if request["op"] == "mail_mark" {
            let key = request["key"]
                .as_str()
                .ok_or("mail_read_invalid")?
                .to_owned();
            return tauri::async_runtime::spawn_blocking(move || {
                app.state::<mail_reads::MailReads>().mark(&key)?;
                #[cfg(feature = "native-tray")]
                tray_mail::marked(&app);
                Ok(Value::Null)
            })
            .await
            .map_err(|_| "mail_read_unavailable")?;
        }
        let public = request["op"] == "announcements";
        let reply = if public {
            role_catalog::revision::announcements().await?
        } else if let Some(bridge) = app.try_state::<role_catalog::revision::RevisionWindow>() {
            bridge
                .request(app.state::<Arc<Catalog>>().inner().clone(), request)
                .await?
        } else {
            return Ok(Value::Null);
        };
        return tauri::async_runtime::spawn_blocking(move || {
            #[cfg(feature = "native-tray")]
            tray_mail::update(&app, &reply, public);
            app.state::<mail_reads::MailReads>().decorate(reply, public)
        })
        .await
        .map_err(|_| "mail_read_unavailable");
    }
    #[cfg(any(
        feature = "revision-loopback",
        all(windows, feature = "legacy-workshop")
    ))]
    if let Some(bridge) = app.try_state::<role_catalog::revision::RevisionWindow>() {
        #[cfg(any(
            all(windows, feature = "pet-win32"),
            all(feature = "pet-gtk-probe", target_os = "linux")
        ))]
        if request["op"] == "enable_pet" {
            let remote = request["remote"]
                .as_str()
                .ok_or("revision_invalid_selection")?;
            let digest = request["digest"]
                .as_str()
                .ok_or("revision_invalid_selection")?;
            bridge.enable_pet(&app, remote, digest).await?;
            return Ok(Value::Null);
        }
        let catalog = app.state::<Arc<Catalog>>().inner().clone();
        return bridge.request(catalog, request).await;
    }
    let _ = app;
    if matches!(
        request["op"].as_str(),
        Some("inspect" | "mailbox" | "announcements")
    ) {
        Ok(Value::Null)
    } else {
        Err("revision_disabled")
    }
}

#[tauri::command]
async fn load_ui_locale(preference: State<'_, Mutex<UiLocale>>) -> Result<String, &'static str> {
    Ok(preference
        .lock()
        .map_err(|_| "ui_locale_unavailable")?
        .current()
        .to_owned())
}

#[tauri::command]
async fn save_ui_locale(app: AppHandle, locale: String) -> Result<(), &'static str> {
    tauri::async_runtime::spawn_blocking(move || {
        let preference = app.state::<Mutex<UiLocale>>();
        let mut language = preference.lock().map_err(|_| "ui_locale_unavailable")?;
        language.save(locale)?;
        if let Some(window) = app.get_webview_window("workshop") {
            let _ = window.set_title(&language.workshop_title());
        }
        if let Some(window) = app.get_webview_window("settings") {
            let _ = window.set_title(&language.message("settings.windowTitle"));
        }
        #[cfg(feature = "native-tray")]
        native_tray::update_language(&app, &language);
        #[cfg(all(windows, feature = "patrol-worker"))]
        patrol_win32::update_language(&app, &language);
        let _ = app.emit("ui-locale-changed", language.current());
        Ok(())
    })
    .await
    .map_err(|_| "ui_locale_unavailable")?
}

#[derive(Default)]
struct SettingsWindow(tauri::async_runtime::Mutex<()>);

#[derive(Default)]
struct WorkshopWindow(tauri::async_runtime::Mutex<()>);

#[tauri::command]
async fn settings_support_id(window: tauri::WebviewWindow) -> Result<Option<String>, &'static str> {
    if window.label() != "settings" {
        return Err("support_id_unavailable");
    }
    #[cfg(all(windows, feature = "legacy-workshop"))]
    {
        tauri::async_runtime::spawn_blocking(|| {
            let home = instance_guard::legacy_home().map_err(|_| "support_id_unavailable")?;
            legacy_session::stored_support_id(&home).map_err(|_| "support_id_unavailable")
        })
        .await
        .map_err(|_| "support_id_unavailable")?
    }
    #[cfg(not(all(windows, feature = "legacy-workshop")))]
    Ok(None)
}

#[tauri::command]
async fn open_redemption_page(window: tauri::WebviewWindow) -> Result<(), &'static str> {
    if !matches!(window.label(), "workshop" | "settings") {
        return Err("redemption_page_unavailable");
    }
    tauri::async_runtime::spawn_blocking(system_folder::open_redemption_page)
        .await
        .map_err(|_| "redemption_page_unavailable")?
        .map_err(|_| "redemption_page_unavailable")
}

#[tauri::command]
async fn open_project_repository(window: tauri::WebviewWindow) -> Result<(), &'static str> {
    if window.label() != "settings" {
        return Err("repository_unavailable");
    }
    tauri::async_runtime::spawn_blocking(system_folder::open_project_repository)
        .await
        .map_err(|_| "repository_unavailable")?
        .map_err(|_| "repository_unavailable")
}

#[tauri::command]
async fn decode_workshop_picture(
    window: tauri::WebviewWindow,
    input: String,
) -> Result<String, &'static str> {
    if window.label() != "workshop" {
        return Err("picture_decode_failed");
    }
    #[cfg(all(windows, feature = "legacy-workshop"))]
    {
        tauri::async_runtime::spawn_blocking(move || picture_import::decode(input))
            .await
            .map_err(|_| "picture_decode_failed")?
    }
    #[cfg(not(all(windows, feature = "legacy-workshop")))]
    {
        let _ = input;
        Err("picture_decode_failed")
    }
}

#[tauri::command]
async fn settings_role_transfer(
    window: tauri::WebviewWindow,
    request: Value,
) -> Result<Value, &'static str> {
    if window.label() != "settings" {
        return Err("role_transfer_unavailable");
    }
    #[cfg(all(windows, feature = "legacy-workshop", feature = "pet-win32"))]
    {
        use role_catalog::revision::{active_role_archive, portable, prepare_imported_pet};
        let app = window.app_handle();
        let library = app
            .path()
            .app_local_data_dir()
            .map_err(|_| "role_transfer_storage")?
            .join("revision-legacy");
        let op = request["op"].as_str().ok_or("role_transfer_invalid")?;
        if op == "list" {
            return tauri::async_runtime::spawn_blocking(move || {
                serde_json::to_value(portable::list(&library)?).map_err(|_| "role_transfer_storage")
            })
            .await
            .map_err(|_| "role_transfer_storage")?;
        }
        if op == "enable" {
            let key = request["key"]
                .as_str()
                .filter(|key| key.len() <= 64)
                .ok_or("role_transfer_invalid")?
                .to_owned();
            let prepared =
                tauri::async_runtime::spawn_blocking(move || prepare_imported_pet(library, &key))
                    .await
                    .map_err(|_| "role_transfer_storage")??;
            show_win32_pet(app, prepared).await?;
            return Ok(Value::Bool(true));
        }
        if !matches!(op, "import" | "export") {
            return Err("role_transfer_invalid");
        }
        let save = op == "export";
        let (send, receive) = tokio::sync::oneshot::channel();
        let owner = window.clone();
        app.run_on_main_thread(move || {
            if save && !pet_win32::is_active() {
                let _ = send.send(Err("role_transfer_unavailable"));
                return;
            }
            let result = owner
                .hwnd()
                .map_err(|_| "role_transfer_dialog")
                .and_then(|hwnd| role_file_dialog::choose(hwnd.0, save));
            let _ = send.send(result);
        })
        .map_err(|_| "role_transfer_dialog")?;
        let Some(path) = receive.await.map_err(|_| "role_transfer_dialog")?? else {
            return Ok(Value::Null);
        };
        let catalog = app.state::<Arc<Catalog>>().inner().clone();
        tauri::async_runtime::spawn_blocking(move || {
            if save {
                if !path
                    .extension()
                    .is_some_and(|ext| ext.eq_ignore_ascii_case("moeguard-role"))
                {
                    return Err("role_transfer_invalid");
                }
                let bytes = active_role_archive(&catalog, &library)?;
                portable::save(&path, &bytes)?;
                Ok(Value::Bool(true))
            } else {
                serde_json::to_value(portable::import(&library, &path)?)
                    .map_err(|_| "role_transfer_storage")
            }
        })
        .await
        .map_err(|_| "role_transfer_storage")?
    }
    #[cfg(not(all(windows, feature = "legacy-workshop", feature = "pet-win32")))]
    {
        let _ = request;
        Err("role_transfer_unavailable")
    }
}

#[tauri::command]
async fn settings_pet(window: tauri::WebviewWindow, request: Value) -> Result<Value, &'static str> {
    if window.label() != "settings" {
        return Err("settings_pet_unavailable");
    }
    #[cfg(all(windows, feature = "legacy-workshop", feature = "pet-win32"))]
    {
        let app = window.app_handle();
        let root = app
            .path()
            .app_local_data_dir()
            .map_err(|_| "settings_pet_unavailable")?;
        let directory = root.join("revision-legacy");
        let op = request["op"].as_str().ok_or("settings_pet_invalid")?;
        if op == "fps" {
            let key = request["key"]
                .as_str()
                .filter(|key| key.len() <= 80)
                .ok_or("settings_pet_invalid")?
                .to_owned();
            let action = request["action"]
                .as_str()
                .filter(|key| key.len() <= 32)
                .ok_or("settings_pet_invalid")?
                .to_owned();
            let fps = request["fps"]
                .as_u64()
                .filter(|fps| (1..=60).contains(fps))
                .ok_or("pet_fps_invalid")? as u8;
            return on_gui_thread(app, "settings_pet_unavailable", move || {
                pet_win32::set_action_fps(&key, &action, fps)?;
                Ok(pet_win32::appearance())
            })
            .await;
        }
        let catalog = app.state::<Arc<Catalog>>().inner().clone();
        if op == "view" {
            let roles = tauri::async_runtime::spawn_blocking(move || {
                let snapshot = WorkshopStore::new(root)
                    .load()
                    .map_err(|_| "settings_pet_unavailable")?;
                let roles = role_catalog::revision::ready_roles(
                    &catalog,
                    &directory,
                    &snapshot.unwrap_or_default()["state"],
                )?;
                Ok::<_, &'static str>(
                    roles
                        .into_iter()
                        .map(|role| serde_json::json!({"key":role.key,"name":role.name}))
                        .collect::<Vec<_>>(),
                )
            })
            .await
            .map_err(|_| "settings_pet_unavailable")??;
            let active = on_gui_thread(app, "settings_pet_unavailable", || {
                Ok(pet_win32::appearance())
            })
            .await?;
            return Ok(serde_json::json!({"roles":roles,"active":active}));
        }
        if op == "switch" {
            let key = request["key"]
                .as_str()
                .filter(|key| key.len() <= 80)
                .ok_or("settings_pet_invalid")?
                .to_owned();
            let prepared = tauri::async_runtime::spawn_blocking(move || {
                let snapshot = WorkshopStore::new(root)
                    .load()
                    .map_err(|_| "settings_pet_unavailable")?;
                role_catalog::revision::ready_roles(
                    &catalog,
                    &directory,
                    &snapshot.unwrap_or_default()["state"],
                )?
                .into_iter()
                .find(|role| role.key == key)
                .ok_or("settings_pet_invalid")?
                .prepare(&catalog, directory)
            })
            .await
            .map_err(|_| "settings_pet_unavailable")??;
            show_win32_pet(app, prepared).await?;
            return on_gui_thread(app, "settings_pet_unavailable", || {
                Ok(pet_win32::appearance())
            })
            .await;
        }
        Err("settings_pet_invalid")
    }
    #[cfg(not(all(windows, feature = "legacy-workshop", feature = "pet-win32")))]
    {
        let _ = request;
        Ok(Value::Null)
    }
}

#[tauri::command]
async fn patrol_settings(
    window: tauri::WebviewWindow,
    request: Value,
) -> Result<Value, &'static str> {
    if window.label() != "settings" {
        return Err("patrol_unavailable");
    }
    #[cfg(all(windows, feature = "patrol-worker"))]
    {
        let (send, receive) = tokio::sync::oneshot::channel();
        let dispatcher = window.clone();
        dispatcher
            .app_handle()
            .run_on_main_thread(move || {
                let result = if request["op"] == "enroll" && !window.is_visible().unwrap_or(false) {
                    Err("patrol_unavailable")
                } else {
                    patrol_win32::setup(request)
                };
                let _ = send.send(result);
            })
            .map_err(|_| "patrol_unavailable")?;
        let mut result = receive.await.map_err(|_| "patrol_unavailable")??;
        if let Some(event) = result.get("open") {
            let leaf = match event {
                Value::Null => None,
                Value::String(leaf) => Some(leaf.clone()),
                _ => return Err("evidence_unavailable"),
            };
            tauri::async_runtime::spawn_blocking(move || {
                patrol_win32::open_evidence(leaf.as_deref())
            })
            .await
            .map_err(|_| "evidence_unavailable")??;
            result = serde_json::json!({"opened":true});
        }
        Ok(result)
    }
    #[cfg(not(all(windows, feature = "patrol-worker")))]
    {
        let _ = request;
        Err("patrol_unsupported")
    }
}

#[tauri::command]
async fn open_workshop(app: AppHandle) -> Result<(), &'static str> {
    let opening = app.state::<WorkshopWindow>();
    let _guard = opening.0.lock().await;
    let window = if let Some(window) = app.get_webview_window("workshop") {
        window
    } else {
        let config = app
            .config()
            .app
            .windows
            .iter()
            .find(|config| config.label == "workshop")
            .ok_or("workshop_open_failed")?;
        let window = WebviewWindowBuilder::from_config(&app, config)
            .and_then(|builder| builder.visible(false).build())
            .map_err(|_| "workshop_open_failed")?;
        let preference = app.state::<Mutex<UiLocale>>();
        let language = preference.lock().map_err(|_| "ui_locale_unavailable")?;
        let _ = window.set_title(&language.workshop_title());
        window
    };
    window.show().map_err(|_| "workshop_open_failed")?;
    window.unminimize().map_err(|_| "workshop_open_failed")?;
    window.set_focus().map_err(|_| "workshop_open_failed")
}

#[tauri::command]
async fn open_settings(
    app: AppHandle,
    settings: State<'_, SettingsWindow>,
) -> Result<(), &'static str> {
    let _guard = settings.0.lock().await;
    let window = if let Some(window) = app.get_webview_window("settings") {
        window
    } else {
        let (width, height) = app
            .primary_monitor()
            .ok()
            .flatten()
            .map_or((780.0, 580.0), |monitor| {
                let size = monitor.work_area().size;
                let scale = monitor.scale_factor();
                let work_width = f64::from(size.width) / scale;
                let work_height = f64::from(size.height) / scale;
                (
                    (work_width * 0.90).clamp(380.0, 780.0),
                    (work_height * 0.88).clamp(310.0, 580.0),
                )
            });
        WebviewWindowBuilder::new(&app, "settings", WebviewUrl::App("settings.html".into()))
            .inner_size(width, height)
            .min_inner_size(380.0, 310.0)
            .center()
            .visible(false)
            .build()
            .map_err(|_| "settings_open_failed")?
    };
    {
        let preference = app.state::<Mutex<UiLocale>>();
        let language = preference.lock().map_err(|_| "ui_locale_unavailable")?;
        let _ = window.set_title(&language.message("settings.windowTitle"));
    }
    window.show().map_err(|_| "settings_open_failed")?;
    window.unminimize().map_err(|_| "settings_open_failed")?;
    window.set_focus().map_err(|_| "settings_open_failed")
}

#[cfg(all(
    windows,
    feature = "pet-win32",
    any(feature = "legacy-workshop", feature = "revision-loopback")
))]
async fn show_win32_pet(
    app: &AppHandle,
    prepared: role_catalog::revision::PreparedPet,
) -> Result<(), &'static str> {
    let (sprite, commit) = tauri::async_runtime::spawn_blocking(move || {
        let key = prepared.playback_key();
        let (frames, commit) = prepared.stage()?;
        Ok::<_, &'static str>((pet_win32::Sprite::from_frames(frames).with_key(key), commit))
    })
    .await
    .map_err(|_| "revision_pet_window_failed")??;
    present_win32_pet(app, sprite, false, commit).await
}

#[cfg(all(windows, feature = "pet-win32"))]
async fn present_win32_pet(
    app: &AppHandle,
    sprite: pet_win32::Sprite,
    fallback: bool,
    commit: impl FnOnce() -> Result<(), &'static str> + Send + 'static,
) -> Result<(), &'static str> {
    let root = app
        .path()
        .app_local_data_dir()
        .map_err(|_| "revision_pet_window_failed")?;
    let (send, receive) = tokio::sync::oneshot::channel();
    app.run_on_main_thread(move || {
        if fallback && pet_win32::is_active() {
            let _ = send.send(Ok(()));
            return;
        }
        let _ = send.send(pet_win32::show(sprite, root, commit));
    })
    .map_err(|_| "revision_pet_window_failed")?;
    receive.await.map_err(|_| "revision_pet_window_failed")??;
    let _ = app.emit_to("settings", "pet-playback-changed", ());
    Ok(())
}

fn app_context(tray: bool) -> tauri::Context<tauri::Wry> {
    let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
    if tray {
        for window in &mut context.config_mut().app.windows {
            window.create = false;
        }
    }
    context
}

#[tauri::command]
async fn load_pet_hotkey(
    window: tauri::WebviewWindow,
) -> Result<Option<(String, bool)>, &'static str> {
    if window.label() != "settings" {
        return Err("pet_hotkey_unavailable");
    }
    #[cfg(all(windows, feature = "pet-win32"))]
    return on_gui_thread(
        window.app_handle(),
        "pet_hotkey_unavailable",
        pet_hotkey::native::read,
    )
    .await
    .map(Some);
    #[cfg(not(all(windows, feature = "pet-win32")))]
    Ok(None)
}

#[tauri::command]
async fn capture_pet_hotkey(
    window: tauri::WebviewWindow,
    recording: bool,
) -> Result<(), &'static str> {
    if window.label() != "settings" {
        return Err("pet_hotkey_unavailable");
    }
    #[cfg(all(windows, feature = "pet-win32"))]
    {
        let app = window.app_handle().clone();
        return on_gui_thread(&app, "pet_hotkey_unavailable", move || {
            pet_hotkey::native::capture(recording.then_some(window))
        })
        .await;
    }
    #[cfg(not(all(windows, feature = "pet-win32")))]
    {
        let _ = recording;
        Err("pet_hotkey_unavailable")
    }
}

#[tauri::command]
async fn save_pet_hotkey(
    window: tauri::WebviewWindow,
    shortcut: String,
) -> Result<(String, bool), &'static str> {
    if window.label() != "settings" || shortcut.len() > 48 {
        return Err("pet_hotkey_invalid");
    }
    #[cfg(all(windows, feature = "pet-win32"))]
    {
        let app = window.app_handle();
        let root = app
            .path()
            .app_local_data_dir()
            .map_err(|_| "pet_hotkey_unavailable")?;
        return on_gui_thread(app, "pet_hotkey_unavailable", move || {
            pet_hotkey::native::save(&root, &shortcut)
        })
        .await;
    }
    #[cfg(not(all(windows, feature = "pet-win32")))]
    Err("pet_hotkey_unavailable")
}

#[cfg(all(windows, feature = "pet-win32"))]
async fn on_gui_thread<T: Send + 'static>(
    app: &AppHandle,
    unavailable: &'static str,
    operation: impl FnOnce() -> Result<T, &'static str> + Send + 'static,
) -> Result<T, &'static str> {
    let (send, receive) = tokio::sync::oneshot::channel();
    app.run_on_main_thread(move || {
        let _ = send.send(operation());
    })
    .map_err(|_| unavailable)?;
    receive.await.map_err(|_| unavailable)?
}

#[tauri::command]
async fn settings_update(app: AppHandle, request: Value) -> Result<Option<Value>, &'static str> {
    #[cfg(all(windows, feature = "windows-update"))]
    {
        windows_update::request(app, request).await
    }
    #[cfg(not(all(windows, feature = "windows-update")))]
    {
        let _ = (app, request);
        Ok(None)
    }
}

fn main() {
    #[cfg(all(windows, feature = "windows-update"))]
    velopack::VelopackApp::build()
        .set_auto_apply_on_startup(false)
        .run();

    #[cfg(all(windows, feature = "legacy-workshop"))]
    let (legacy_home, _application_lock) = {
        let home = instance_guard::legacy_home()
            .and_then(|home| instance_guard::acquire(&home).map(|guard| (home, guard)));
        match home {
            Ok((home, Some(guard))) => (home, guard),
            Ok((_, None)) => return,
            Err(_) => {
                eprintln!("Could not acquire the MoeGuard application lock");
                std::process::exit(1);
            }
        }
    };
    // The development identifier isolates these drafts from shipped client data.
    let app = tauri::Builder::default()
        .setup(move |app| {
            let root = app.path().app_local_data_dir()?;
            let language = UiLocale::open(&root);
            if let Some(window) = app.get_webview_window("workshop") {
                let _ = window.set_title(&language.workshop_title());
            }
            #[cfg(feature = "native-tray")]
            native_tray::install(app.handle(), &language)?;
            #[cfg(all(windows, feature = "patrol-worker"))]
            patrol_win32::update_language(app.handle(), &language);
            app.manage(Mutex::new(language));
            #[cfg(all(windows, feature = "windows-update"))]
            windows_update::start(app.handle(), &root);
            app.manage(SettingsWindow::default());
            app.manage(WorkshopWindow::default());
            app.manage(Arc::new(Catalog::production(&root)));
            #[cfg(feature = "revision-loopback")]
            app.manage(mail_reads::MailReads::new(root.clone(), None));
            #[cfg(all(windows, feature = "legacy-workshop"))]
            app.manage(mail_reads::MailReads::new(root.clone(), Some(&legacy_home)));
            #[cfg(feature = "revision-loopback")]
            if let Some(bridge) = role_catalog::revision::RevisionWindow::open(&root) {
                app.manage(bridge);
            }
            #[cfg(all(windows, feature = "legacy-workshop"))]
            app.manage(role_catalog::revision::RevisionWindow::legacy(
                &root,
                Some(legacy_home.clone()),
            ));
            #[cfg(all(feature = "native-tray", feature = "revision-loopback"))]
            tray_mail::begin(app.handle().clone(), root.clone(), None);
            #[cfg(all(feature = "native-tray", windows, feature = "legacy-workshop"))]
            tray_mail::begin(app.handle().clone(), root.clone(), Some(legacy_home));
            #[cfg(all(windows, feature = "pet-win32"))]
            {
                let _ = pet_hotkey::native::start(&root);
                let host = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    #[cfg(any(feature = "legacy-workshop", feature = "revision-loopback"))]
                    {
                        let folder = if cfg!(feature = "legacy-workshop") {
                            "revision-legacy"
                        } else {
                            "revision-loopback"
                        };
                        let catalog = host.state::<Arc<Catalog>>().inner().clone();
                        let restored = tauri::async_runtime::spawn_blocking(move || {
                            let removed = WorkshopStore::new(root.clone())
                                .load()
                                .ok()
                                .flatten()
                                .map(|snapshot| removed_workshop_roles(&snapshot["state"]))
                                .unwrap_or_default();
                            let allow_bundled = !removed.iter().any(|id| id == "whale");
                            let directory = root.join(folder);
                            let sprite =
                                role_catalog::revision::clear_removed_pet(&directory, &removed)
                                    .and_then(|cleared| {
                                        if cleared {
                                            Ok(None)
                                        } else {
                                            let key = role_catalog::revision::restore_playback_key(
                                                &directory,
                                            )?;
                                            role_catalog::revision::restore_pet(
                                                &catalog, &directory,
                                            )
                                            .map(
                                                |frames| {
                                                    frames.map(|frames| {
                                                        pet_win32::Sprite::from_frames(frames)
                                                            .with_key(key.unwrap_or_else(|| {
                                                                "catalog:whale".into()
                                                            }))
                                                    })
                                                },
                                            )
                                        }
                                    });
                            (sprite, allow_bundled)
                        })
                        .await;
                        if let Ok((sprite, allow_bundled)) = restored {
                            if let Ok(Some(sprite)) = sprite {
                                if present_win32_pet(&host, sprite, true, || Ok(()))
                                    .await
                                    .is_ok()
                                {
                                    return;
                                }
                            }
                            if !allow_bundled {
                                return;
                            }
                        }
                    }
                    let sprite = tauri::async_runtime::spawn_blocking(|| {
                        role_catalog::bundled_pet().map(pet_win32::Sprite::from_frames)
                    })
                    .await;
                    match sprite {
                        Ok(Ok(sprite)) => {
                            if present_win32_pet(&host, sprite, true, || Ok(()))
                                .await
                                .is_err()
                            {
                                eprintln!("Could not show the bundled companion");
                            }
                        }
                        _ => eprintln!("Could not load the bundled companion"),
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            load_workshop,
            save_workshop,
            catalog_inventory,
            workshop_action_preview,
            catalog_download,
            catalog_cancel,
            workshop_revision,
            load_ui_locale,
            save_ui_locale,
            load_pet_hotkey,
            capture_pet_hotkey,
            save_pet_hotkey,
            open_settings,
            open_workshop,
            settings_support_id,
            open_redemption_page,
            open_project_repository,
            decode_workshop_picture,
            patrol_settings,
            settings_role_transfer,
            settings_pet,
            settings_update
        ])
        .build(app_context(cfg!(feature = "native-tray")))
        .expect("failed to launch the workshop development client");
    #[cfg(feature = "native-tray")]
    app.run(|app, event| {
        #[cfg(all(windows, feature = "patrol-worker"))]
        if let tauri::RunEvent::WindowEvent {
            label,
            event: tauri::WindowEvent::Destroyed,
            ..
        } = &event
        {
            if label == "settings" {
                patrol_win32::cancel_setup();
            }
        }
        #[cfg(all(windows, feature = "pet-win32"))]
        if matches!(event, tauri::RunEvent::Exit) {
            #[cfg(feature = "patrol-worker")]
            patrol_win32::shutdown();
            pet_hotkey::native::stop();
            pet_win32::close();
        }
        native_tray::handle_run_event(app, event);
    });
    #[cfg(not(feature = "native-tray"))]
    app.run(|_, _| {});
}

#[cfg(test)]
mod startup_tests {
    #[cfg(all(target_os = "linux", feature = "pet-gtk-probe"))]
    #[test]
    fn bundled_whale_decodes_without_a_service_or_installed_library() {
        let frames = crate::role_catalog::bundled_pet().expect("verified built-in sprite");
        assert_eq!(
            frames.summary(),
            serde_json::json!({"idle_frames": 12, "reaction_frames": 8, "fps": 6})
        );
        assert!(frames.idle[0].chunks_exact(4).any(|pixel| pixel[3] == 0));
        assert!(frames.idle[0].chunks_exact(4).any(|pixel| pixel[3] == 255));
    }

    #[test]
    fn tray_launch_does_not_create_a_webview() {
        let context = super::app_context(true);
        let workshop = &context.config().app.windows[0];
        assert_eq!(workshop.label, "workshop");
        assert!(
            !workshop.create,
            "tray startup must not load the workshop WebView"
        );
        assert!(super::app_context(false).config().app.windows[0].create);
    }
}
