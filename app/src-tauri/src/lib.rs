mod auth;

use serde::Serialize;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{AppHandle, State};

use auth::{AuthState, PublicAccount};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const MANIFEST_URL: &str = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json";

pub(crate) fn user_agent() -> String {
    format!("RastamanPoland/zorza-client/{VERSION} (github.com/RastamanPoland/zorza-client)")
}

/// Ten sam folder co w wersji w Pythonie, zeby profile, mody i logowanie byly wspolne.
pub fn root_dir() -> PathBuf {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    if cfg!(target_os = "windows") {
        dirs::config_dir().unwrap_or(home).join(".zorza") // %APPDATA%\.zorza
    } else if cfg!(target_os = "macos") {
        home.join("Library").join("Application Support").join("zorza")
    } else {
        home.join(".zorza")
    }
}

#[derive(Serialize)]
struct AppInfo {
    version: String,
    os: String,
    data_dir: String,
}

#[tauri::command]
fn app_info() -> AppInfo {
    AppInfo {
        version: VERSION.to_string(),
        os: std::env::consts::OS.to_string(),
        data_dir: root_dir().display().to_string(),
    }
}

/// Najnowsze wydanie Minecrafta z manifestu Mojangu (test sieci i parsowania JSON).
#[tauri::command]
async fn latest_minecraft() -> Result<String, String> {
    let client = reqwest::Client::builder()
        .user_agent(user_agent())
        .build()
        .map_err(|e| e.to_string())?;
    let manifest: serde_json::Value = client
        .get(MANIFEST_URL)
        .send()
        .await
        .map_err(|e| format!("Brak połączenia z serwerami Mojangu: {e}"))?
        .error_for_status()
        .map_err(|e| e.to_string())?
        .json()
        .await
        .map_err(|e| e.to_string())?;
    manifest["latest"]["release"]
        .as_str()
        .map(str::to_string)
        .ok_or_else(|| "Nieoczekiwana odpowiedź Mojangu".to_string())
}

/// Logowanie. Gdy potrzebny jest kod, okno dostaje zdarzenie "login-code".
#[tauri::command]
async fn login(app: AppHandle, state: State<'_, AuthState>) -> Result<PublicAccount, String> {
    let cancel = Arc::new(AtomicBool::new(false));
    if let Some(old) = state.cancel.lock().unwrap().replace(cancel.clone()) {
        old.store(true, Ordering::Relaxed); // tylko jedno logowanie naraz
    }
    let result = auth::login(&app, &cancel).await;
    {
        let mut current = state.cancel.lock().unwrap();
        if current.as_ref().is_some_and(|c| Arc::ptr_eq(c, &cancel)) {
            *current = None;
        }
    }
    let account = result?;
    let public = PublicAccount::from(&account);
    *state.account.lock().unwrap() = Some(account);
    Ok(public)
}

#[tauri::command]
fn cancel_login(state: State<'_, AuthState>) {
    if let Some(c) = state.cancel.lock().unwrap().take() {
        c.store(true, Ordering::Relaxed);
    }
}

#[tauri::command]
fn logout(state: State<'_, AuthState>) {
    auth::forget_saved_login();
    *state.account.lock().unwrap() = None;
}

#[tauri::command]
fn has_saved_login() -> bool {
    auth::has_saved_login()
}

/// Otwiera strone w domyslnej przegladarce (tylko https).
#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err("Można otwierać tylko adresy https".into());
    }
    tauri_plugin_opener::open_url(url, None::<&str>).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AuthState::default())
        .invoke_handler(tauri::generate_handler![
            app_info,
            latest_minecraft,
            login,
            cancel_login,
            logout,
            has_saved_login,
            open_url
        ])
        .run(tauri::generate_context!())
        .expect("nie udało się uruchomić Zorza Client");
}
