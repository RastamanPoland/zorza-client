#[macro_use]
mod i18n;
mod auth;
mod content;
mod game;
mod net;
mod profiles;

use serde::Serialize;
use serde_json::{Map, Value};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, State};

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
        .map_err(|e| tr!("Cannot reach Mojang servers: {e}", "Brak połączenia z serwerami Mojangu: {e}"))?
        .error_for_status()
        .map_err(|e| e.to_string())?
        .json()
        .await
        .map_err(|e| e.to_string())?;
    manifest["latest"]["release"]
        .as_str()
        .map(str::to_string)
        .ok_or_else(|| tr!("Unexpected response from Mojang", "Nieoczekiwana odpowiedź Mojangu"))
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
        return Err(tr!("Only https addresses can be opened", "Można otwierać tylko adresy https"));
    }
    tauri_plugin_opener::open_url(url, None::<&str>).map_err(|e| e.to_string())
}

// ------------------------------------------------------------- profile i ustawienia

#[tauri::command]
fn get_profiles() -> Value {
    profiles::load_profiles()
}

#[tauri::command]
fn save_profiles(data: Value) -> Result<(), String> {
    profiles::save_profiles(&data)
}

#[tauri::command]
fn get_config() -> Value {
    let mut cfg = profiles::load_config();
    if let Some(obj) = cfg.as_object_mut() {
        obj.remove("msa_client_id"); // oknu niepotrzebne
    }
    cfg
}

#[tauri::command]
fn save_config(update: Map<String, Value>) -> Result<(), String> {
    let cfg = profiles::save_config(&update)?;
    i18n::set_language(cfg["language"].as_str().unwrap_or("en"));
    Ok(())
}

#[tauri::command]
async fn list_versions() -> Result<Vec<String>, String> {
    game::release_versions(&net::client()?).await
}

#[tauri::command]
async fn copy_profile(src: String, dst: String) -> Result<(), String> {
    if !(src == "default" || profiles::safe_pid(&src)) || !profiles::safe_pid(&dst) || dst == "default" {
        return Err(tr!("Invalid profile ID", "Nieprawidłowy identyfikator profilu"));
    }
    let (from, to) = (profiles::profile_dir(&src), profiles::profile_dir(&dst));
    tauri::async_runtime::spawn_blocking(move || profiles::copy_dir(&from, &to))
        .await
        .map_err(|e| e.to_string())?
}

/// Otwiera folder: "" = folder launchera, inaczej folder profilu (opcjonalnie podfolder mods itp.).
#[tauri::command]
fn open_folder(pid: String, sub: Option<String>) -> Result<(), String> {
    let mut path = if pid.is_empty() {
        root_dir()
    } else if pid == "default" || profiles::safe_pid(&pid) {
        profiles::profile_dir(&pid)
    } else {
        return Err(tr!("Invalid profile", "Nieprawidłowy profil"));
    };
    if let Some(sub) = sub {
        if !matches!(sub.as_str(), "mods" | "resourcepacks" | "saves" | "shaderpacks" | "config") {
            return Err(tr!("Unknown folder", "Nieznany folder"));
        }
        path = path.join(sub);
    }
    std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
    tauri_plugin_opener::open_path(path, None::<&str>).map_err(|e| e.to_string())
}

// ----------------------------------------------------------------- mody i paczki

fn checked_dir(pid: &str) -> Result<PathBuf, String> {
    if pid == "default" || profiles::safe_pid(pid) {
        Ok(profiles::profile_dir(pid))
    } else {
        Err(tr!("Invalid profile", "Nieprawidłowy profil"))
    }
}

fn profile_of(pid: &str) -> Result<Value, String> {
    profiles::load_profiles()["profiles"].get(pid).cloned().ok_or_else(|| tr!("No such profile", "Nie ma takiego profilu"))
}

/// "latest" -> numer najnowszego wydania.
async fn concrete_version(v: &str) -> Result<String, String> {
    if v != "latest" {
        return Ok(v.to_string());
    }
    let manifest = net::get_json(&net::client()?, "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json").await?;
    manifest["latest"]["release"].as_str().map(str::to_string).ok_or_else(|| tr!("No latest version", "Brak najnowszej wersji"))
}

fn world_dir(pid: &str, world: &str) -> Result<PathBuf, String> {
    if world.is_empty() || world.contains('/') || world.contains('\\') || world == "." || world == ".." {
        return Err(tr!("Invalid world name", "Nieprawidłowa nazwa świata"));
    }
    let dir = checked_dir(pid)?.join("saves").join(world);
    if !dir.join("level.dat").exists() {
        return Err(tr!("No such world", "Nie ma takiego świata"));
    }
    Ok(dir)
}

#[tauri::command]
async fn search_content(
    kind: String,
    source: String,
    query: String,
    pid: String,
    sort: String,
    offset: usize,
) -> Result<(String, Vec<content::Hit>), String> {
    let p = profile_of(&pid)?;
    let version = concrete_version(p["game_version"].as_str().unwrap_or("latest")).await?;
    let loader = p["loader"].as_str().unwrap_or("fabric").to_string();
    let hits = content::search(&kind, &source, query.trim(), &version, &loader, &sort, offset).await?;
    Ok((version, hits))
}

#[tauri::command]
async fn project_details(hit: content::Hit) -> Result<content::Details, String> {
    content::details(&hit).await
}

#[tauri::command]
async fn install_content(
    pid: String,
    kind: String,
    hit: content::Hit,
    world: Option<String>,
    game_state: State<'_, GameState>,
) -> Result<Vec<String>, String> {
    if game_state.busy.lock().unwrap().as_deref() == Some(pid.as_str()) {
        return Err(tr!("Close the game before installing.", "Zamknij grę przed instalowaniem."));
    }
    let dir = checked_dir(&pid)?;
    let p = profile_of(&pid)?;
    let version = concrete_version(p["game_version"].as_str().unwrap_or("latest")).await?;
    match kind.as_str() {
        "mod" => content::install_mod(&hit, &version, &dir, p["loader"].as_str().unwrap_or("fabric")).await,
        "resourcepack" => {
            content::install_pack(&hit, &kind, &version, &dir.join("resourcepacks"), &content::resourcepack_index(&dir)).await
        }
        "datapack" => {
            let world = world_dir(&pid, world.as_deref().unwrap_or_default())?;
            content::install_pack(&hit, &kind, &version, &world.join("datapacks"), &content::datapack_index(&world)).await
        }
        _ => Err(tr!("Unknown type", "Nieznany typ")),
    }
}

/// Instaluje modpack do nowego folderu profilu `pid`. Profil dopisuje okno po sukcesie.
#[tauri::command]
async fn install_modpack(app: AppHandle, pid: String, hit: content::Hit) -> Result<content::ModpackInfo, String> {
    if !profiles::safe_pid(&pid) || pid == "default" {
        return Err(tr!("Invalid profile ID", "Nieprawidłowy identyfikator profilu"));
    }
    let dir = profiles::profile_dir(&pid);
    if dir.exists() {
        return Err(tr!("Folder {} already exists", "Folder {} już istnieje", dir.display()));
    }
    let reporter = AppReporter { app };
    let result = content::install_modpack(&hit, &dir, &reporter).await;
    if result.is_err() {
        let _ = std::fs::remove_dir_all(&dir); // folder utworzony wylacznie przez te instalacje
    }
    result
}

#[tauri::command]
fn installed_mods(pid: String) -> Result<Vec<content::InstalledFile>, String> {
    Ok(content::list_mods(&checked_dir(&pid)?))
}

#[tauri::command]
fn toggle_mod(pid: String, file: String) -> Result<bool, String> {
    content::toggle_mod(&checked_dir(&pid)?, &file)
}

#[tauri::command]
fn remove_mod(pid: String, file: String) -> Result<(), String> {
    content::remove_mod(&checked_dir(&pid)?, &file)
}

#[tauri::command]
fn installed_packs(pid: String) -> Result<Vec<content::InstalledFile>, String> {
    let dir = checked_dir(&pid)?;
    Ok(content::list_packs(&dir.join("resourcepacks"), &content::resourcepack_index(&dir)))
}

#[tauri::command]
fn remove_resourcepack(pid: String, file: String) -> Result<(), String> {
    let dir = checked_dir(&pid)?;
    content::remove_pack(&dir.join("resourcepacks"), &content::resourcepack_index(&dir), &file)
}

#[tauri::command]
fn list_worlds(pid: String) -> Result<Vec<String>, String> {
    Ok(content::list_worlds(&checked_dir(&pid)?))
}

#[tauri::command]
fn installed_keys(pid: String, kind: String) -> Result<Vec<String>, String> {
    Ok(content::installed_keys(&checked_dir(&pid)?, &kind))
}

#[tauri::command]
async fn loader_versions(loader: String, game_version: String) -> Result<Vec<game::LoaderVersion>, String> {
    game::loader_versions(&net::client()?, &loader, &game_version).await
}

#[tauri::command]
fn has_curseforge_key() -> bool {
    !content::cf_key().is_empty()
}

// -------------------------------------------------------------------------- gra

#[derive(Default)]
struct GameState {
    child: Arc<Mutex<Option<Child>>>,
    busy: Arc<Mutex<Option<String>>>, // id profilu, ktory sie uruchamia albo dziala
}

struct AppReporter {
    app: AppHandle,
}

impl game::Reporter for AppReporter {
    fn status(&self, text: &str) {
        let _ = self.app.emit("status", text);
    }
    fn progress(&self, done: usize, total: usize) {
        if done == total || done % 20 == 0 {
            let _ = self.app.emit("progress", (done, total));
        }
    }
}

fn launch_cfg(profile: &Value, cfg: &Value) -> game::LaunchCfg {
    let text = |v: &Value| v.as_str().unwrap_or_default().to_string();
    game::LaunchCfg {
        game_version: profile["game_version"].as_str().unwrap_or("latest").to_string(),
        loader: profile["loader"].as_str().unwrap_or("fabric").to_string(),
        loader_version: text(&profile["loader_version"]),
        memory_mb: profile["memory_mb"].as_u64().or_else(|| cfg["memory_mb"].as_u64()).unwrap_or(4096),
        jvm_args: text(&profile["jvm_args"]),
        java_path: text(&cfg["java_path"]),
    }
}

fn spawn_reader<R: std::io::Read + Send + 'static>(app: AppHandle, stream: R) {
    std::thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            let _ = app.emit("game-log", line);
        }
    });
}

#[tauri::command]
async fn launch(
    app: AppHandle,
    pid: String,
    auth: State<'_, AuthState>,
    game_state: State<'_, GameState>,
) -> Result<(), String> {
    let account = auth.account.lock().unwrap().clone().ok_or_else(|| tr!("Sign in first", "Najpierw się zaloguj"))?;
    {
        let mut busy = game_state.busy.lock().unwrap();
        if busy.is_some() {
            return Err(tr!("The game is already running or starting", "Gra już działa albo właśnie się uruchamia"));
        }
        *busy = Some(pid.clone());
    }
    let busy = game_state.busy.clone();
    let child_slot = game_state.child.clone();
    let result = start_game(&app, &pid, &account, child_slot.clone(), busy.clone()).await;
    if result.is_err() {
        *busy.lock().unwrap() = None;
    }
    result
}

async fn start_game(
    app: &AppHandle,
    pid: &str,
    account: &auth::Account,
    child_slot: Arc<Mutex<Option<Child>>>,
    busy: Arc<Mutex<Option<String>>>,
) -> Result<(), String> {
    let data = profiles::load_profiles();
    let profile = data["profiles"].get(pid).cloned().ok_or_else(|| tr!("No such profile", "Nie ma takiego profilu"))?;
    let cfg = launch_cfg(&profile, &profiles::load_config());
    let game_dir = profiles::profile_dir(pid);
    let reporter = AppReporter { app: app.clone() };
    let cmd = game::prepare(&cfg, account, &game_dir, &reporter).await?;
    let _ = app.emit("status", tr!("Starting the game as {}…", "Uruchamiam grę jako {}…", account.name));
    let mut command = Command::new(&cmd[0]);
    command.args(&cmd[1..]).current_dir(&game_dir).stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // bez okna konsoli
    }
    let mut child = command.spawn().map_err(|e| tr!("Could not start Java ({}): {e}", "Nie udało się uruchomić Javy ({}): {e}", cmd[0]))?;
    if let Some(out) = child.stdout.take() {
        spawn_reader(app.clone(), out);
    }
    if let Some(err) = child.stderr.take() {
        spawn_reader(app.clone(), err);
    }
    *child_slot.lock().unwrap() = Some(child);
    let _ = app.emit("game-started", pid);
    let app = app.clone();
    std::thread::spawn(move || {
        let code = loop {
            std::thread::sleep(Duration::from_millis(500));
            let mut slot = child_slot.lock().unwrap();
            match slot.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(status))) => {
                    *slot = None;
                    break status.code();
                }
                Some(Ok(None)) => {}
                _ => {
                    *slot = None;
                    break None;
                }
            }
        };
        *busy.lock().unwrap() = None;
        let _ = app.emit("game-exit", code);
    });
    Ok(())
}

#[tauri::command]
fn stop_game(game_state: State<'_, GameState>) -> Result<(), String> {
    match game_state.child.lock().unwrap().as_mut() {
        Some(child) => child.kill().map_err(|e| tr!("Could not stop the game: {e}", "Nie udało się zatrzymać gry: {e}")),
        None => Err(tr!("The game is not running", "Gra nie działa")),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    i18n::set_language(profiles::load_config()["language"].as_str().unwrap_or("en"));
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AuthState::default())
        .manage(GameState::default())
        .invoke_handler(tauri::generate_handler![
            app_info,
            latest_minecraft,
            login,
            cancel_login,
            logout,
            has_saved_login,
            open_url,
            get_profiles,
            save_profiles,
            get_config,
            save_config,
            list_versions,
            copy_profile,
            open_folder,
            launch,
            stop_game,
            search_content,
            project_details,
            install_content,
            install_modpack,
            installed_mods,
            toggle_mod,
            remove_mod,
            installed_packs,
            remove_resourcepack,
            list_worlds,
            installed_keys,
            loader_versions,
            has_curseforge_key
        ])
        .run(tauri::generate_context!())
        .expect("failed to start Zorza Client");
}
