use serde::Serialize;
use std::path::PathBuf;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const MANIFEST_URL: &str = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json";

fn user_agent() -> String {
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![app_info, latest_minecraft])
        .run(tauri::generate_context!())
        .expect("nie udało się uruchomić Zorza Client");
}
