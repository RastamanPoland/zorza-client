//! profiles.json i config.json w tym samym formacie co wersja w Pythonie.

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

use crate::root_dir;

pub const LOADERS: [&str; 5] = ["vanilla", "fabric", "quilt", "forge", "neoforge"];

fn profiles_file() -> PathBuf {
    root_dir().join("profiles.json")
}

fn config_file() -> PathBuf {
    root_dir().join("config.json")
}

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

fn write_json(path: &Path, v: &Value) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let text = serde_json::to_string_pretty(v).map_err(|e| e.to_string())?;
    std::fs::write(path, text).map_err(|e| format!("Nie można zapisać {}: {e}", path.display()))
}

pub fn safe_pid(pid: &str) -> bool {
    !pid.is_empty() && pid.len() <= 80 && pid.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

pub fn profile_dir(pid: &str) -> PathBuf {
    if pid == "default" {
        root_dir().join("game")
    } else {
        root_dir().join("profiles").join(pid)
    }
}

pub fn load_profiles() -> Value {
    let mut data = read_json(&profiles_file()).filter(|v| v["profiles"].as_object().is_some_and(|p| !p.is_empty()));
    let mut changed = data.is_none();
    let mut data = data.take().unwrap_or_else(|| {
        json!({"selected": "default", "profiles": {"default": {"name": "Domyślny", "game_version": "latest", "loader": "fabric"}}})
    });
    if let Some(profiles) = data["profiles"].as_object_mut() {
        for p in profiles.values_mut() {
            let Some(p) = p.as_object_mut() else { continue };
            let loader_ok = p.get("loader").and_then(Value::as_str).is_some_and(|l| LOADERS.contains(&l));
            if !loader_ok {
                let fabric = p.get("use_fabric").and_then(Value::as_bool).unwrap_or(true);
                p.insert("loader".into(), json!(if fabric { "fabric" } else { "vanilla" }));
                changed = true;
            }
            if p.remove("use_fabric").is_some() {
                changed = true;
            }
            if let Some(fl) = p.remove("fabric_loader") {
                if fl.as_str().is_some_and(|s| !s.is_empty()) {
                    p.insert("loader_version".into(), fl);
                }
                changed = true;
            }
        }
    }
    let selected_ok = data["selected"].as_str().is_some_and(|s| data["profiles"].get(s).is_some());
    if !selected_ok {
        let first = data["profiles"].as_object().and_then(|p| p.keys().next().cloned()).unwrap_or_default();
        data["selected"] = json!(first);
        changed = true;
    }
    if changed {
        let _ = write_json(&profiles_file(), &data);
    }
    data
}

pub fn save_profiles(data: &Value) -> Result<(), String> {
    let profiles = data["profiles"].as_object().ok_or("Nieprawidłowe dane profili")?;
    if profiles.is_empty() {
        return Err("Musi zostać co najmniej jeden profil".into());
    }
    for (pid, p) in profiles {
        if pid != "default" && !safe_pid(pid) {
            return Err(format!("Nieprawidłowy identyfikator profilu: {pid}"));
        }
        if !p["name"].is_string() || !p["game_version"].is_string() {
            return Err("Profil musi mieć nazwę i wersję gry".into());
        }
    }
    write_json(&profiles_file(), data)
}

pub fn load_config() -> Value {
    let mut cfg = read_json(&config_file()).filter(Value::is_object).unwrap_or_else(|| json!({}));
    let defaults = [
        ("memory_mb", json!(4096)),
        ("java_path", json!("")),
        ("curseforge_api_key", json!("")),
    ];
    let obj = cfg.as_object_mut().unwrap();
    for (k, v) in defaults {
        obj.entry(k).or_insert(v);
    }
    cfg
}

/// Zapisuje tylko pola z ustawien; reszta pliku (np. msa_client_id) zostaje.
pub fn save_config(update: &Map<String, Value>) -> Result<Value, String> {
    let mut cfg = load_config();
    let obj = cfg.as_object_mut().unwrap();
    for key in ["memory_mb", "java_path", "curseforge_api_key"] {
        if let Some(v) = update.get(key) {
            obj.insert(key.into(), v.clone());
        }
    }
    let ram = obj["memory_mb"].as_u64().unwrap_or(0);
    if !(1024..=65536).contains(&ram) {
        return Err("Pamięć RAM musi być w zakresie 1024–65536 MB".into());
    }
    write_json(&config_file(), &cfg)?;
    Ok(cfg)
}

/// Kopia folderu profilu bez logow i raportow bledow.
pub fn copy_dir(src: &Path, dst: &Path) -> Result<(), String> {
    if dst.exists() {
        return Err(format!("Folder {} już istnieje", dst.display()));
    }
    std::fs::create_dir_all(dst).map_err(|e| e.to_string())?;
    if !src.exists() {
        return std::fs::create_dir_all(dst.join("mods")).map_err(|e| e.to_string());
    }
    for entry in std::fs::read_dir(src).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name();
        if name == "logs" || name == "crash-reports" {
            continue;
        }
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        if kind.is_dir() {
            copy_dir(&entry.path(), &dst.join(&name))?;
        } else if kind.is_file() {
            std::fs::copy(entry.path(), dst.join(&name)).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}
