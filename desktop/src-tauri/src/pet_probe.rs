//! Isolated Linux proof: draw a verified installed role in a native window, not a WebView.
use crate::pet_frames::{Frames, SIZE};
use gtk::{cairo, gdk, gdk::prelude::GdkContextExt, gdk_pixbuf, glib, prelude::*};
use std::{cell::RefCell, rc::Rc, sync::Mutex, time::Duration};
use tauri::{AppHandle, Manager, WebviewWindowBuilder, WindowBuilder};

const WINDOW_ERROR: &str = "revision_pet_window_failed";
const PET: &str = "pet-probe";
type Result<T> = std::result::Result<T, &'static str>;

#[cfg(test)]
#[test]
fn layered_pixels_are_bgra_premultiplied_and_erase_hidden_rgb() {
    let rgba = [255, 121, 1, 0, 100, 150, 200, 128, 10, 20, 30, 255];
    assert_eq!(
        crate::pet_frames::layered_pixels(&rgba),
        [0, 0, 0, 0, 100, 75, 50, 128, 30, 20, 10, 255]
    );
}

struct Playback {
    idle: Vec<gdk_pixbuf::Pixbuf>,
    reaction: Vec<gdk_pixbuf::Pixbuf>,
    index: usize,
    reacting: bool,
}

impl Playback {
    fn pixbufs(frames: Vec<Vec<u8>>) -> Vec<gdk_pixbuf::Pixbuf> {
        frames
            .into_iter()
            .map(|frame| {
                gdk_pixbuf::Pixbuf::from_bytes(
                    &glib::Bytes::from_owned(frame),
                    gdk_pixbuf::Colorspace::Rgb,
                    true,
                    8,
                    SIZE as i32,
                    SIZE as i32,
                    4 * SIZE as i32,
                )
            })
            .collect()
    }
    fn tick(&mut self) {
        self.index += 1;
        if self.reacting && self.index >= self.reaction.len() {
            self.reacting = false;
            self.index = 0;
        } else if !self.reacting {
            self.index %= self.idle.len();
        }
    }
}

pub(crate) async fn show(app: &AppHandle, frames: Frames) -> Result<()> {
    let (send, recv) = tokio::sync::oneshot::channel();
    let host = app.clone();
    app.run_on_main_thread(move || {
        let _ = send.send(show_window(&host, frames));
    })
    .map_err(|_| WINDOW_ERROR)?;
    recv.await.map_err(|_| WINDOW_ERROR)?
}

fn show_window(app: &AppHandle, frames: Frames) -> Result<()> {
    if let Some(window) = app.get_window(PET) {
        return window.show().map_err(|_| WINDOW_ERROR);
    }
    let interval = Duration::from_millis(1000 / frames.fps);
    let playback = Rc::new(RefCell::new(Playback {
        idle: Playback::pixbufs(frames.idle),
        reaction: Playback::pixbufs(frames.reaction),
        index: 0,
        reacting: false,
    }));
    let window = WindowBuilder::new(app, PET)
        .title("MoeGuard Pet Probe")
        .inner_size(224., 224.)
        .position(30., 30.)
        .resizable(false)
        .decorations(false)
        .transparent(true)
        .always_on_top(true)
        .skip_taskbar(true)
        .focused(false)
        .visible(false)
        .build()
        .map_err(|_| WINDOW_ERROR)?;
    let contents = match window.default_vbox() {
        Ok(contents) => contents,
        Err(_) => {
            let _ = window.destroy();
            return Err(WINDOW_ERROR);
        }
    };
    let area = gtk::DrawingArea::new();
    contents.pack_start(&area, true, true, 0);
    area.set_app_paintable(true);
    area.add_events(gdk::EventMask::BUTTON_PRESS_MASK);
    let draw = playback.clone();
    area.connect_draw(move |widget, cr| {
        let frames = draw.borrow();
        let sequence = if frames.reacting {
            &frames.reaction
        } else {
            &frames.idle
        };
        let frame = &sequence[frames.index];
        cr.set_operator(cairo::Operator::Clear);
        let _ = cr.paint();
        cr.set_operator(cairo::Operator::Over);
        cr.scale(
            widget.allocated_width() as f64 / SIZE as f64,
            widget.allocated_height() as f64 / SIZE as f64,
        );
        cr.set_source_pixbuf(frame, 0., 0.);
        let _ = cr.paint();
        glib::Propagation::Stop
    });
    let host = app.clone();
    let on_press = playback.clone();
    area.connect_button_press_event(move |widget, event| {
        match event.button() {
            1 => {
                let mut state = on_press.borrow_mut();
                state.reacting = true;
                state.index = 0;
                widget.queue_draw();
            }
            3 => show_menu(&host),
            _ => return glib::Propagation::Proceed,
        }
        glib::Propagation::Stop
    });
    let weak = area.downgrade();
    let tick = glib::timeout_add_local(interval, move || {
        let Some(widget) = weak.upgrade() else {
            return glib::ControlFlow::Break;
        };
        playback.borrow_mut().tick();
        widget.queue_draw();
        glib::ControlFlow::Continue
    });
    let tick = RefCell::new(Some(tick));
    area.connect_destroy(move |_| {
        if let Some(source) = tick.take() {
            source.remove();
        }
    });
    area.show();
    if window.show().is_err() {
        let _ = window.destroy();
        return Err(WINDOW_ERROR);
    }
    Ok(())
}

fn localized(app: &AppHandle, key: &str) -> String {
    app.state::<Mutex<crate::ui_locale::UiLocale>>()
        .lock()
        .map(|locale| locale.message(key))
        .unwrap_or_default()
}

fn show_menu(app: &AppHandle) {
    let menu = gtk::Menu::new();
    let open = gtk::MenuItem::with_label(&localized(app, "petProbe.open"));
    let close = gtk::MenuItem::with_label(&localized(app, "petProbe.close"));
    let reopen = app.clone();
    open.connect_activate(move |_| {
        if let Some(workshop) = reopen.get_webview_window("workshop") {
            let _ = workshop.unminimize();
            let _ = workshop.set_focus();
        } else if let Some(config) = reopen
            .config()
            .app
            .windows
            .iter()
            .find(|config| config.label == "workshop")
        {
            let title = localized(&reopen, "windowTitle");
            let _ = WebviewWindowBuilder::from_config(&reopen, config)
                .and_then(|builder| builder.title(title).build());
        }
    });
    let host = app.clone();
    close.connect_activate(move |_| {
        if let Some(window) = host.get_window(PET) {
            let _ = window.destroy();
        }
    });
    menu.append(&open);
    menu.append(&close);
    // Keep the popup alive until it dismisses without retaining the native window.
    let keep = Rc::new(RefCell::new(Some(menu.clone())));
    menu.connect_selection_done(move |_| {
        keep.take();
    });
    menu.show_all();
    menu.popup_at_pointer(None);
}
