fn main() {
    let manifest = tauri_build::AppManifest::new().commands(&[
        "load_workshop",
        "save_workshop",
        "catalog_inventory",
        "workshop_action_preview",
        "catalog_download",
        "catalog_cancel",
        "workshop_revision",
        "load_ui_locale",
        "save_ui_locale",
        "load_pet_hotkey",
        "save_pet_hotkey",
        "open_settings",
        "open_workshop",
        "settings_support_id",
        "open_redemption_page",
        "open_project_repository",
        "decode_workshop_picture",
        "patrol_settings",
        "settings_role_transfer",
        "settings_pet",
        "settings_update",
        "capture_pet_hotkey",
    ]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest))
        .expect("workshop build configuration is invalid");
}
