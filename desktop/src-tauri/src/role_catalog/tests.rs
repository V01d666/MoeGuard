use super::*;
use std::{io::Read, net::TcpListener, sync::mpsc, thread};

fn server(
    status: &str,
    body: &[u8],
    stalled: bool,
) -> (String, mpsc::Receiver<()>, mpsc::Sender<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let (started, received) = mpsc::channel();
    let (release, held) = mpsc::channel();
    let body = body.to_vec();
    let status = status.to_owned();
    thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(4)))
            .unwrap();
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            let mut byte = [0];
            socket.read_exact(&mut byte).unwrap();
            request.push(byte[0]);
        }
        let request = String::from_utf8(request).unwrap();
        assert!(request.starts_with("GET /arona.zip HTTP/1.1\r\n"));
        let lower = request.to_ascii_lowercase();
        assert!(!lower.contains("authorization:") && !lower.contains("cookie:"));
        write!(socket, "HTTP/1.1 {status}\r\nContent-Length: {}\r\nLocation: /not-a-pack\r\nConnection: close\r\n\r\n", body.len()).unwrap();
        let _ = socket.write_all(&body[..1]);
        let _ = started.send(());
        if stalled {
            let _ = held.recv_timeout(Duration::from_secs(5));
        }
        let _ = socket.write_all(&body[1..]);
    });
    (base, received, release)
}

const ARONA: &[u8] = include_bytes!("../../../test/fixtures/catalog-role.zip");

#[cfg(all(windows, feature = "pet-win32", feature = "legacy-workshop"))]
#[test]
fn action_previews_use_installed_frames_and_preserve_playback() {
    let directory = tempfile::tempdir().unwrap();
    let catalog = Catalog::production(directory.path());
    let whale = catalog
        .action_preview("whale", "click_reaction", Some(60))
        .unwrap();
    let mut preview = png::Decoder::new(Cursor::new(whale)).read_info().unwrap();
    let animation = preview.info().animation_control.unwrap();
    assert_eq!((animation.num_frames, animation.num_plays), (8, 1));
    let mut first = vec![0; preview.output_buffer_size().unwrap()];
    let mut next = first.clone();
    preview.next_frame(&mut first).unwrap();
    let delay = preview.info().frame_control.unwrap();
    assert_eq!((delay.delay_num, delay.delay_den), (1, 60));
    preview.next_frame(&mut next).unwrap();
    assert_ne!(first, next);

    assert!(catalog.action_preview("arona", "idle", None).is_err());
    fs::create_dir(&catalog.directory).unwrap();
    fs::write(catalog.directory.join("arona.zip"), ARONA).unwrap();
    let arona = catalog.action_preview("arona", "idle", None).unwrap();
    let preview = png::Decoder::new(Cursor::new(arona)).read_info().unwrap();
    assert_eq!(preview.info().animation_control.unwrap().num_plays, 0);
    assert!(catalog
        .action_preview("arona", "../role.json", None)
        .is_err());
}

#[test]
fn installs_verified_bytes_and_restores_without_network() {
    tauri::async_runtime::block_on(async {
        let temp = tempfile::tempdir().unwrap();
        let (base, _, _) = server("200 OK", ARONA, false);
        let catalog = Arc::new(Catalog::new(temp.path(), base));
        let mut progress = Vec::new();
        let role = catalog
            .download("arona", |value| progress.push(value))
            .await
            .unwrap();
        assert_eq!(role.id, "arona");
        assert!(role.portrait.src.starts_with("data:image/png;base64,"));
        assert!(progress.windows(2).all(|pair| pair[0] < pair[1]));
        assert_eq!(
            fs::read(catalog.directory.join("arona.zip")).unwrap(),
            ARONA
        );
        let reopened = Arc::new(Catalog::new(temp.path(), "http://127.0.0.1:1".into()));
        assert_eq!(reopened.inventory().unwrap().len(), 2);
        assert_eq!(
            reopened.download("whale", |_| {}).await.unwrap().id,
            "whale"
        );
        assert_eq!(
            reopened.download("arona", |_| {}).await.unwrap().id,
            "arona"
        );
        fs::write(reopened.directory.join("arona.zip"), b"bad-cache").unwrap();
        assert_eq!(reopened.inventory().unwrap().len(), 1);
    });
}

#[test]
fn rejects_changed_bytes_redirect_and_unknown_id() {
    tauri::async_runtime::block_on(async {
        let temp = tempfile::tempdir().unwrap();
        for (status, body, expected) in [
            (
                "200 OK",
                {
                    let mut changed = ARONA.to_vec();
                    changed[0] ^= 1;
                    changed
                },
                "catalog_integrity",
            ),
            ("200 OK", vec![0], "catalog_integrity"),
            ("302 Found", vec![0], "catalog_network"),
        ] {
            let (base, _, _) = server(status, &body, false);
            let catalog = Arc::new(Catalog::new(temp.path(), base));
            assert_eq!(
                catalog.download("arona", |_| {}).await.unwrap_err(),
                expected
            );
            assert_eq!(
                catalog.download("../arona", |_| {}).await.unwrap_err(),
                "catalog_unknown"
            );
            assert!(!catalog.directory.join("arona.zip").exists());
        }
    });
}

#[test]
fn cancellation_interrupts_stalled_response_without_publishing() {
    tauri::async_runtime::block_on(async {
        let temp = tempfile::tempdir().unwrap();
        let (base, started, release) = server("200 OK", ARONA, true);
        let catalog = Arc::new(Catalog::new(temp.path(), base));
        let worker = catalog.clone();
        let pending =
            tauri::async_runtime::spawn(async move { worker.download("arona", |_| {}).await });
        started.recv_timeout(Duration::from_secs(4)).unwrap();
        assert_eq!(
            catalog.download("poppy", |_| {}).await.unwrap_err(),
            "catalog_busy"
        );
        assert!(catalog.cancel("arona"));
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), pending)
                .await
                .unwrap()
                .unwrap()
                .unwrap_err(),
            "catalog_cancelled"
        );
        let _ = release.send(());
        assert!(!catalog.directory.join("arona.zip").exists());
        assert!(!catalog.cancel("arona"));
    });
}
