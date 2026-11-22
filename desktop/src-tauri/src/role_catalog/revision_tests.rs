use super::revision::*;
use super::*;

#[test]
fn plans_real_add_and_revision_from_the_builtin_idle() {
    let temp = tempfile::tempdir().unwrap();
    let catalog = Catalog::production(temp.path());
    let edited = prepare(
        &catalog,
        "whale",
        Mode::Modify,
        vec!["idle".into()],
        "  放慢一点  ",
    )
    .unwrap();
    assert_eq!(edited.instruction(), "放慢一点");
    assert_eq!(edited.spec()["operation"], "action_revision");
    assert_eq!(edited.spec()["profile_id"], "pet-8af04aee88f590ed");
    let added = prepare(
        &catalog,
        "whale",
        Mode::Add,
        vec!["welcome".into(), "notice".into()],
        "",
    )
    .unwrap();
    assert_eq!(
        added.spec()["actions"],
        serde_json::json!(["notice", "welcome"])
    );
    assert_eq!(
        edited.spec()["identity_sha256"],
        added.spec()["identity_sha256"]
    );
    assert!(prepare(&catalog, "whale", Mode::Add, vec!["idle".into()], "").is_err());
    assert!(prepare(&catalog, "whale", Mode::Modify, vec!["notice".into()], "").is_err());
    assert!(prepare(&catalog, "whale", Mode::Add, vec!["notice".into(); 2], "").is_err());
    assert!(prepare(
        &catalog,
        "whale",
        Mode::Modify,
        vec!["idle".into(), "click_reaction".into()],
        ""
    )
    .is_err());
    assert!(prepare(&catalog, "whale", Mode::Add, vec!["drag_pickup".into()], "").is_err());
    assert!(prepare(&catalog, "arona", Mode::Modify, vec!["idle".into()], "").is_err());
}

#[test]
fn prepared_downloaded_source_rejects_corruption_even_at_the_original_length() {
    let temp = tempfile::tempdir().unwrap();
    let catalog = Catalog::production(temp.path());
    fs::create_dir(&catalog.directory).unwrap();
    let path = catalog.directory.join("arona.zip");
    let mut bytes = include_bytes!("../../../test/fixtures/catalog-role.zip").to_vec();
    fs::write(&path, &bytes).unwrap();
    assert!(prepare(
        &catalog,
        "arona",
        Mode::Modify,
        vec!["click_reaction".into()],
        ""
    )
    .is_ok());
    bytes[0] ^= 1;
    fs::write(path, &bytes).unwrap();
    assert!(prepare(
        &catalog,
        "arona",
        Mode::Modify,
        vec!["click_reaction".into()],
        ""
    )
    .is_err());
}

// Invoked in a new process for each round trip by the Python HTTP contract test.
#[test]
#[ignore = "requires isolated Python HTTP test service"]
fn existing_service_driver() {
    let root = PathBuf::from(std::env::var_os("MG_EDIT_TEST_ROOT").unwrap());
    let catalog = Arc::new(Catalog::production(&root));
    let operation = std::env::var("MG_EDIT_TEST_OPERATION").unwrap();
    let marks = crate::mail_reads::MailReads::new(root.clone(), Some(&root.join("legacy-home")));
    let result = tauri::async_runtime::block_on(async {
        #[cfg(all(feature = "pet-gtk-probe", target_os = "linux"))]
        if operation == "restore_pet" {
            return super::revision::restore_pet(&catalog, &root.join("revision-loopback"))
                .map(|frames| frames.map_or(serde_json::Value::Null, |frames| frames.summary()));
        }
        if operation == "library_list" {
            return installed_pets(&root.join("revision-loopback"));
        }
        #[cfg(all(feature = "pet-gtk-probe", target_os = "linux"))]
        if operation.starts_with("library_") {
            let library = root.join("revision-loopback");
            let remote = std::env::var("MG_EDIT_TEST_REMOTE").unwrap();
            let digest = std::env::var("MG_EDIT_TEST_DIGEST").unwrap();
            let (frames, commit) = installed_pet(library, &remote, &digest)?.stage()?;
            commit()?;
            return Ok(frames.summary());
        }
        #[cfg(all(feature = "pet-gtk-probe", target_os = "linux"))]
        if operation == "catalog_enable" {
            let id = std::env::var("MG_EDIT_TEST_ACTION").unwrap();
            let (frames, commit) =
                prepare_catalog_pet(&catalog, root.join("revision-loopback"), &id)?.stage()?;
            commit()?;
            return Ok(frames.summary());
        }
        if operation == "announcements" {
            return announcements().await.map(|view| marks.decorate(view, true));
        }
        if operation == "mail_mark" {
            let input: serde_json::Value =
                serde_json::from_str(&std::env::var("MG_EDIT_TEST_MAIL").unwrap()).unwrap();
            return marks
                .mark(input["key"].as_str().unwrap())
                .map(|_| serde_json::Value::Null);
        }
        #[cfg(not(windows))]
        if let Some(operation) = operation.strip_prefix("legacy_") {
            let (operation, renew) = operation
                .strip_prefix("renew_")
                .map_or((operation, false), |op| (op, true));
            let request = match operation {
                "inspect" => serde_json::json!({"op":"inspect"}),
                "mailbox" => serde_json::json!({"op":"mailbox"}),
                "resume" => serde_json::json!({"op":"resume"}),
                "submit" => serde_json::json!({"op":"submit","id":"whale","mode":"add",
                    "actions":["notice"],"instruction":""}),
                _ => panic!("unknown legacy test operation"),
            };
            return RevisionWindow::legacy_fixture(&root, renew)
                .request(catalog, request)
                .await;
        }
        let service = EditService::from_test_env(&root).unwrap();
        #[cfg(all(feature = "pet-gtk-probe", target_os = "linux"))]
        if matches!(operation.as_str(), "select_pet" | "stage_pet") {
            let remote = std::env::var("MG_EDIT_TEST_REMOTE").unwrap();
            let digest = std::env::var("MG_EDIT_TEST_DIGEST").unwrap();
            let prepared = service.prepare_pet(&remote, &digest).await?;
            let (frames, commit) = prepared.stage()?;
            if operation == "select_pet" {
                commit()?;
            }
            return Ok(frames.summary());
        }
        if matches!(
            operation.as_str(),
            "mailbox_send"
                | "credit_balance"
                | "credit_redeem"
                | "submit_portraits"
                | "edit_portrait"
                | "submit_initial"
        ) {
            let mut request: serde_json::Value =
                serde_json::from_str(&std::env::var("MG_EDIT_TEST_MAIL").unwrap()).unwrap();
            request["op"] = serde_json::json!(operation);
            return RevisionWindow::open(&root)
                .unwrap()
                .request(catalog, request)
                .await;
        }
        if matches!(
            operation.as_str(),
            "inspect"
                | "mailbox"
                | "preview"
                | "install"
                | "prepare_pet"
                | "dismiss"
                | "cancel"
                | "finish"
                | "save_motion"
                | "discard_motion_edit"
                | "portrait_gallery"
                | "finish_portraits"
        ) {
            let action = std::env::var("MG_EDIT_TEST_ACTION").unwrap_or_default();
            let remote = std::env::var("MG_EDIT_TEST_REMOTE").unwrap_or_default();
            let digest = std::env::var("MG_EDIT_TEST_DIGEST").unwrap_or_default();
            let outcome = RevisionWindow::open(&root)
                .unwrap()
                .request(catalog, serde_json::json!({"op":operation,"action":action,"remote":remote,"digest":digest}))
                .await;
            return if operation == "mailbox" {
                outcome.map(|view| marks.decorate(view, false))
            } else {
                outcome
            };
        }
        if operation == "confirm" {
            let remote = std::env::var("MG_EDIT_TEST_REMOTE").unwrap();
            let digest = std::env::var("MG_EDIT_TEST_DIGEST").unwrap();
            return service
                .confirm(&remote, &digest)
                .await
                .map(|result| serde_json::to_value(result).unwrap());
        }
        if operation == "collect" {
            return service
                .collect()
                .await
                .map(|result| serde_json::to_value(result).unwrap());
        }
        match operation.as_str() {
            "modify" => {
                service
                    .start(
                        catalog,
                        "arona".into(),
                        Mode::Modify,
                        vec!["click_reaction".into()],
                        "动作幅度小一点".into(),
                    )
                    .await
            }
            "add" => {
                service
                    .start(
                        catalog,
                        "whale".into(),
                        Mode::Add,
                        vec!["welcome".into(), "notice".into()],
                        "轻轻挥手".into(),
                    )
                    .await
            }
            "resume" => service.resume().await,
            _ => panic!("unknown test operation"),
        }
        .map(|snapshot| serde_json::to_value(snapshot).unwrap())
    });
    println!(
        "EDIT_RESULT:{}",
        match result {
            Ok(value) => serde_json::to_string(&value).unwrap(),
            Err(code) => serde_json::json!({"error":code}).to_string(),
        }
    );
}
