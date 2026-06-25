use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
};
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem},
    tray::TrayIconBuilder,
    ActivationPolicy, AppHandle, Emitter, Manager, State,
};

struct WatcherState {
    child: Mutex<Option<Child>>,
}

fn repo_root(app: &AppHandle) -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|error| error.to_string())?;
    let candidates = [
        std::env::current_dir().ok(),
        exe.parent().map(Path::to_path_buf),
        exe.parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .map(Path::to_path_buf),
        app.path().resource_dir().ok(),
        app.path().resource_dir().ok().map(|path| path.join("_up_")),
    ];

    for candidate in candidates.into_iter().flatten() {
        let script = candidate.join("llama-screen.py");
        if script.exists() {
            return Ok(candidate);
        }
    }

    Err("Could not locate llama-screen.py".into())
}

fn engine_script(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(repo_root(app)?.join("llama-screen.py"))
}

fn app_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_data_dir().map_err(|error| error.to_string())?;
    std::fs::create_dir_all(&dir).map_err(|error| error.to_string())?;
    Ok(dir)
}

fn run_engine(app: &AppHandle, args: &[&str]) -> Result<String, String> {
    let output = Command::new("python3")
        .arg(engine_script(app)?)
        .args(args)
        .env("CRONSNAP_DATA_DIR", app_data_dir(app)?)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| error.to_string())?;

    if output.status.success() {
        String::from_utf8(output.stdout).map_err(|error| error.to_string())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Err(if stderr.is_empty() { stdout } else { stderr })
    }
}

fn run_engine_json(app: &AppHandle, args: &[&str]) -> Result<Value, String> {
    let output = run_engine(app, args)?;
    serde_json::from_str(&output).map_err(|error| error.to_string())
}

fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[tauri::command]
fn engine_status(app: AppHandle) -> Result<Value, String> {
    run_engine_json(&app, &["status"])
}

#[tauri::command]
fn engine_archive(app: AppHandle, days: u16) -> Result<Value, String> {
    let days_arg = days.to_string();
    run_engine_json(&app, &["archive", "--days", &days_arg])
}

#[tauri::command]
fn engine_ocr(app: AppHandle, allow_full_screen_capture: bool) -> Result<Value, String> {
    if allow_full_screen_capture {
        run_engine_json(&app, &["ocr", "--format", "json", "--allow-full-screen-capture"])
    } else {
        run_engine_json(&app, &["ocr", "--format", "json"])
    }
}

#[tauri::command]
fn engine_report(app: AppHandle, day: String) -> Result<(), String> {
    run_engine(&app, &["report", &day]).map(|_| ())
}

#[tauri::command]
fn open_data_dir(app: AppHandle) -> Result<(), String> {
    Command::new("open")
        .arg(app_data_dir(&app)?)
        .status()
        .map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
fn start_watcher(app: AppHandle, state: State<WatcherState>) -> Result<(), String> {
    let mut child = state.child.lock().map_err(|error| error.to_string())?;
    if child.as_mut().is_some_and(|process| process.try_wait().ok().flatten().is_none()) {
        return Ok(());
    }

    let process = Command::new("python3")
        .arg(engine_script(&app)?)
        .env("CRONSNAP_DATA_DIR", app_data_dir(&app)?)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| error.to_string())?;
    *child = Some(process);
    Ok(())
}

#[tauri::command]
fn stop_watcher(state: State<WatcherState>) -> Result<(), String> {
    let mut child = state.child.lock().map_err(|error| error.to_string())?;
    if let Some(mut process) = child.take() {
        let _ = process.kill();
        let _ = process.wait();
    }
    Ok(())
}

fn setup_tray(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let open = MenuItemBuilder::with_id("open", "Open Archive").build(app)?;
    let start = MenuItemBuilder::with_id("start", "Start Watcher").build(app)?;
    let pause = MenuItemBuilder::with_id("pause", "Pause Watcher").build(app)?;
    let ocr = MenuItemBuilder::with_id("ocr", "OCR Active Window").build(app)?;
    let settings = MenuItemBuilder::with_id("settings", "Settings").build(app)?;
    let today = MenuItemBuilder::with_id("today", "Generate Today").build(app)?;
    let yesterday = MenuItemBuilder::with_id("yesterday", "Generate Yesterday").build(app)?;
    let data = MenuItemBuilder::with_id("data", "Open Data Folder").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
    let separator = PredefinedMenuItem::separator(app)?;

    let menu = MenuBuilder::new(app)
        .items(&[&open, &separator, &start, &pause, &ocr, &today, &yesterday, &data, &separator, &quit])
        .items(&[&settings])
        .build()?;

    TrayIconBuilder::with_id("cronsnap")
        .tooltip("CronSnap")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => show_main(app),
            "start" => {
                let state = app.state::<WatcherState>();
                let _ = start_watcher(app.clone(), state);
                let _ = app.emit("cronsnap://watching", true);
                let _ = app.emit("cronsnap://refresh", ());
            }
            "pause" => {
                let state = app.state::<WatcherState>();
                let _ = stop_watcher(state);
                let _ = app.emit("cronsnap://watching", false);
                let _ = app.emit("cronsnap://refresh", ());
            }
            "ocr" => {
                show_main(app);
                let _ = app.emit("cronsnap://ocr", ());
            }
            "settings" => {
                show_main(app);
                let _ = app.emit("cronsnap://settings", ());
            }
            "today" => {
                let _ = run_engine(app, &["report", "today"]);
                let _ = app.emit("cronsnap://refresh", ());
            }
            "yesterday" => {
                let _ = run_engine(app, &["report", "yesterday"]);
                let _ = app.emit("cronsnap://refresh", ());
            }
            "data" => {
                let _ = open_data_dir(app.clone());
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;

    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .manage(WatcherState {
            child: Mutex::new(None),
        })
        .setup(|app| {
            app.set_activation_policy(ActivationPolicy::Accessory);
            setup_tray(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            engine_status,
            engine_archive,
            engine_ocr,
            engine_report,
            open_data_dir,
            start_watcher,
            stop_watcher
        ])
        .run(tauri::generate_context!())
        .expect("error while running CronSnap");
}
