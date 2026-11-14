//! Przygotowanie gry: wersja Mojangu + Fabric/Quilt, biblioteki, zasoby, Java, komenda uruchomienia.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::auth::Account;
use crate::net::{download, download_all, get_json, Task};
use crate::root_dir;

const MANIFEST_URL: &str = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json";
const RESOURCES_URL: &str = "https://resources.download.minecraft.net";
const JAVA_RUNTIMES_URL: &str =
    "https://piston-meta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json";
const FABRIC_META: &str = "https://meta.fabricmc.net/v2";
const QUILT_META: &str = "https://meta.quiltmc.org/v3";

pub const CP_SEP: &str = if cfg!(windows) { ";" } else { ":" };

pub struct LaunchCfg {
    pub game_version: String,
    pub loader: String,
    pub loader_version: String,
    pub memory_mb: u64,
    pub jvm_args: String,
    pub java_path: String,
}

/// Raportowanie postepu do okna.
pub trait Reporter: Sync {
    fn status(&self, text: &str);
    fn progress(&self, done: usize, total: usize);
}

fn libs() -> PathBuf {
    root_dir().join("libraries")
}
fn assets() -> PathBuf {
    root_dir().join("assets")
}
fn versions() -> PathBuf {
    root_dir().join("versions")
}

pub fn os_name() -> &'static str {
    if cfg!(windows) {
        "windows"
    } else if cfg!(target_os = "macos") {
        "osx"
    } else {
        "linux"
    }
}

fn arch() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => "x64",
        "aarch64" => "arm64",
        "x86" => "x86",
        _ => "x64",
    }
}

/// Reguly Mojangu: ostatnia pasujaca decyduje, brak regul = dozwolone. Zadne "features" nie sa wlaczone.
pub fn rules_allow(rules: &Value) -> bool {
    let Some(list) = rules.as_array() else { return true };
    if list.is_empty() {
        return true;
    }
    let mut allowed = false;
    for rule in list {
        let mut ok = true;
        if let Some(os) = rule.get("os") {
            if os.get("name").and_then(Value::as_str).is_some_and(|n| n != os_name()) {
                ok = false;
            }
            if os.get("arch").and_then(Value::as_str).is_some_and(|a| a != arch()) {
                ok = false;
            }
        }
        if let Some(features) = rule.get("features").and_then(Value::as_object) {
            if features.values().any(|v| v.as_bool() == Some(true)) {
                ok = false;
            }
        }
        if ok {
            allowed = rule.get("action").and_then(Value::as_str) == Some("allow");
        }
    }
    allowed
}

/// 'grupa:artefakt:wersja[:klasyfikator][@ext]' -> sciezka w repozytorium.
pub fn maven_path(name: &str) -> String {
    let (name, ext) = name.split_once('@').unwrap_or((name, "jar"));
    let parts: Vec<&str> = name.split(':').collect();
    if parts.len() < 3 {
        return name.replace(':', "/");
    }
    let classifier = parts.get(3).map(|c| format!("-{c}")).unwrap_or_default();
    format!(
        "{}/{}/{}/{}-{}{}.{}",
        parts[0].replace('.', "/"),
        parts[1],
        parts[2],
        parts[1],
        parts[2],
        classifier,
        ext
    )
}

/// Nazwa biblioteki bez wersji: pozniejszy wpis (np. z Fabrica) nadpisuje wczesniejszy.
fn lib_key(name: &str) -> String {
    let parts: Vec<&str> = name.split('@').next().unwrap_or(name).split(':').collect();
    let mut key: Vec<&str> = parts.iter().take(2).copied().collect();
    if let Some(c) = parts.get(3) {
        key.push(c);
    }
    key.join(":")
}

fn arr(v: &Value) -> Vec<Value> {
    v.as_array().cloned().unwrap_or_default()
}

fn merge_inherited(parent: &mut Value, child: &Value) {
    for key in ["mainClass", "javaVersion", "assetIndex", "assets"] {
        if !child[key].is_null() {
            parent[key] = child[key].clone();
        }
    }
    let mut libraries = arr(&parent["libraries"]);
    libraries.extend(arr(&child["libraries"]));
    parent["libraries"] = Value::Array(libraries);
    for kind in ["jvm", "game"] {
        let mut list = arr(&parent["arguments"][kind]);
        list.extend(arr(&child["arguments"][kind]));
        parent["arguments"][kind] = Value::Array(list);
    }
}

fn enc(s: &str) -> String {
    s.chars()
        .map(|c| if c.is_ascii_alphanumeric() || "-._~".contains(c) { c.to_string() } else { format!("%{:02X}", c as u32) })
        .collect()
}

pub async fn release_versions(client: &reqwest::Client) -> Result<Vec<String>, String> {
    let manifest = get_json(client, MANIFEST_URL).await?;
    let mut out = Vec::new();
    for v in arr(&manifest["versions"]) {
        if v["type"] != "release" {
            continue;
        }
        let id = v["id"].as_str().unwrap_or_default();
        let mut parts = id.split('.').map(|p| p.parse::<u32>());
        if let (Some(Ok(major)), minor) = (parts.next(), parts.next()) {
            let minor = minor.and_then(Result::ok).unwrap_or(0);
            if major > 1 || (major == 1 && minor >= 19) {
                out.push(id.to_string());
            }
        }
    }
    Ok(out)
}

async fn resolve_version(client: &reqwest::Client, cfg: &LaunchCfg, rep: &dyn Reporter) -> Result<Value, String> {
    let manifest = get_json(client, MANIFEST_URL).await?;
    let vid = if cfg.game_version == "latest" {
        manifest["latest"]["release"].as_str().ok_or("Brak najnowszej wersji w manifeście")?.to_string()
    } else {
        cfg.game_version.clone()
    };
    let entry = arr(&manifest["versions"])
        .into_iter()
        .find(|v| v["id"] == vid.as_str())
        .ok_or_else(|| format!("Nie ma wersji {vid} w manifeście Mojangu"))?;
    let vfile = versions().join(&vid).join(format!("{vid}.json"));
    download(
        client,
        &Task {
            url: entry["url"].as_str().unwrap_or_default().to_string(),
            dest: vfile.clone(),
            sha1: entry["sha1"].as_str().map(str::to_string),
            size: None,
        },
    )
    .await?;
    let text = std::fs::read_to_string(&vfile).map_err(|e| e.to_string())?;
    let mut v: Value = serde_json::from_str(&text).map_err(|e| format!("Uszkodzony plik wersji: {e}"))?;
    if v["arguments"].is_null() {
        return Err("Ta wersja gry jest za stara (obsługiwane 1.19+)".into());
    }
    v["_id"] = json!(vid);
    v["_mc"] = json!(vid);
    match cfg.loader.as_str() {
        "fabric" | "quilt" => {
            let fabric = cfg.loader == "fabric";
            let (meta, label) = if fabric { (FABRIC_META, "Fabric Loader") } else { (QUILT_META, "Quilt Loader") };
            let loaders = arr(&get_json(client, &format!("{meta}/versions/loader/{}", enc(&vid))).await?);
            if loaders.is_empty() {
                return Err(format!("{label} nie wspiera jeszcze wersji {vid}. Wybierz starszą wersję gry albo inny loader."));
            }
            let version_of = |l: &Value| l["loader"]["version"].as_str().unwrap_or_default().to_string();
            let pinned = loaders.iter().find(|l| !cfg.loader_version.is_empty() && version_of(l) == cfg.loader_version);
            if !cfg.loader_version.is_empty() && pinned.is_none() {
                rep.status(&format!("{label} {} nie pasuje do wersji {vid}, używam najnowszego", cfg.loader_version));
            }
            let pick = pinned
                .or_else(|| {
                    loaders.iter().find(|l| {
                        if fabric {
                            l["loader"]["stable"].as_bool() == Some(true)
                        } else {
                            let s = version_of(l);
                            !(s.contains("beta") || s.contains("pre") || s.contains("rc"))
                        }
                    })
                })
                .unwrap_or(&loaders[0]);
            let lv = version_of(pick);
            rep.status(&format!("{label} {lv}"));
            let profile =
                get_json(client, &format!("{meta}/versions/loader/{}/{}/profile/json", enc(&vid), enc(&lv))).await?;
            merge_inherited(&mut v, &profile);
            v["_loader"] = json!([cfg.loader, lv]);
        }
        "forge" | "neoforge" => {
            return Err("Forge i NeoForge będą w nowej wersji w kolejnym kroku. Na razie użyj wersji w Pythonie.".into())
        }
        _ => {}
    }
    Ok(v)
}

/// Zwraca (zadania pobierania, classpath, jary z natywnymi bibliotekami).
fn collect_libraries(v: &Value) -> (Vec<Task>, Vec<PathBuf>, Vec<PathBuf>) {
    let mut order: Vec<String> = Vec::new();
    let mut chosen: HashMap<String, (Option<Task>, PathBuf)> = HashMap::new();
    for lib in arr(&v["libraries"]) {
        let Some(name) = lib["name"].as_str() else { continue };
        if !rules_allow(&lib["rules"]) {
            continue;
        }
        let art = &lib["downloads"]["artifact"];
        let entry = if art.is_object() {
            let path = libs().join(art["path"].as_str().map(str::to_string).unwrap_or_else(|| maven_path(name)));
            let url = art["url"].as_str().unwrap_or_default();
            let task = (!url.is_empty()).then(|| Task {
                url: url.to_string(),
                dest: path.clone(),
                sha1: art["sha1"].as_str().map(str::to_string),
                size: art["size"].as_u64(),
            });
            (task, path)
        } else if let Some(repo) = lib["url"].as_str() {
            let rel = maven_path(name);
            let path = libs().join(&rel);
            let task = Task {
                url: format!("{}/{}", repo.trim_end_matches('/'), rel),
                dest: path.clone(),
                sha1: lib["sha1"].as_str().map(str::to_string),
                size: lib["size"].as_u64(),
            };
            (Some(task), path)
        } else if lib["downloads"].is_object() {
            continue; // stare wpisy z samymi natywnymi plikami
        } else {
            (None, libs().join(maven_path(name)))
        };
        let key = lib_key(name);
        if !chosen.contains_key(&key) {
            order.push(key.clone());
        }
        chosen.insert(key, entry);
    }
    let mut tasks = Vec::new();
    let mut classpath = Vec::new();
    let mut natives = Vec::new();
    for key in order {
        let (task, path) = chosen.remove(&key).unwrap();
        if key.contains(":natives-") {
            natives.push(path.clone());
        }
        if let Some(t) = task {
            tasks.push(t);
        }
        classpath.push(path);
    }
    (tasks, classpath, natives)
}

async fn collect_assets(client: &reqwest::Client, v: &Value) -> Result<Vec<Task>, String> {
    let idx = &v["assetIndex"];
    let id = idx["id"].as_str().ok_or("Brak indeksu zasobów")?;
    let idx_file = assets().join("indexes").join(format!("{id}.json"));
    download(
        client,
        &Task {
            url: idx["url"].as_str().unwrap_or_default().to_string(),
            dest: idx_file.clone(),
            sha1: idx["sha1"].as_str().map(str::to_string),
            size: None,
        },
    )
    .await?;
    let index: Value = serde_json::from_str(&std::fs::read_to_string(&idx_file).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let mut tasks = Vec::new();
    if let Some(objects) = index["objects"].as_object() {
        for obj in objects.values() {
            let Some(h) = obj["hash"].as_str() else { continue };
            if h.len() < 2 {
                continue;
            }
            tasks.push(Task {
                url: format!("{RESOURCES_URL}/{}/{h}", &h[..2]),
                dest: assets().join("objects").join(&h[..2]).join(h),
                sha1: Some(h.to_string()),
                size: obj["size"].as_u64(),
            });
        }
    }
    Ok(tasks)
}

fn java_platform_key() -> Option<&'static str> {
    Some(match (os_name(), arch()) {
        ("windows", "x64") => "windows-x64",
        ("windows", "x86") => "windows-x86",
        ("windows", "arm64") => "windows-arm64",
        ("osx", "x64") => "mac-os",
        ("osx", "arm64") => "mac-os-arm64",
        ("linux", "x64") => "linux",
        ("linux", "x86") => "linux-i386",
        _ => return None,
    })
}

async fn ensure_java(client: &reqwest::Client, v: &Value, cfg: &LaunchCfg, rep: &dyn Reporter) -> Result<String, String> {
    if !cfg.java_path.trim().is_empty() {
        return Ok(cfg.java_path.trim().to_string());
    }
    let component = v["javaVersion"]["component"].as_str().unwrap_or("java-runtime-gamma").to_string();
    let major = v["javaVersion"]["majorVersion"].as_u64().map(|m| m.to_string()).unwrap_or("?".into());
    let exe = if cfg!(windows) { "java.exe" } else { "java" };
    let Some(key) = java_platform_key() else {
        return Err(format!("Mojang nie ma Javy {major} dla tego systemu. Zainstaluj ją i wpisz ścieżkę w Ustawieniach."));
    };
    let all = get_json(client, JAVA_RUNTIMES_URL).await?;
    let entries = arr(&all[key][component.as_str()]);
    let Some(entry) = entries.first() else {
        return Err(format!("Mojang nie ma Javy {major} dla tego systemu. Zainstaluj ją i wpisz ścieżkę w Ustawieniach."));
    };
    let home = root_dir().join("runtimes").join(&component).join(key);
    let manifest = get_json(client, entry["manifest"]["url"].as_str().unwrap_or_default()).await?;
    let files = manifest["files"].as_object().cloned().unwrap_or_default();
    let mut tasks = Vec::new();
    let mut executables = Vec::new();
    for (rel, info) in &files {
        let target = home.join(rel);
        match info["type"].as_str() {
            Some("directory") => {
                let _ = std::fs::create_dir_all(&target);
            }
            Some("file") => {
                let raw = &info["downloads"]["raw"];
                tasks.push(Task {
                    url: raw["url"].as_str().unwrap_or_default().to_string(),
                    dest: target.clone(),
                    sha1: raw["sha1"].as_str().map(str::to_string),
                    size: raw["size"].as_u64(),
                });
                if info["executable"].as_bool() == Some(true) {
                    executables.push(target);
                }
            }
            _ => {}
        }
    }
    rep.status(&format!("Java {major} ({component})"));
    download_all(client, tasks, 12, |d, t| rep.progress(d, t)).await?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for (rel, info) in &files {
            if info["type"] == "link" {
                let link = home.join(rel);
                if !link.exists() {
                    if let (Some(dir), Some(target)) = (link.parent(), info["target"].as_str()) {
                        let _ = std::fs::create_dir_all(dir);
                        let _ = std::os::unix::fs::symlink(target, &link);
                    }
                }
            }
        }
        for f in executables {
            let _ = std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o755));
        }
    }
    #[cfg(not(unix))]
    let _ = executables;
    for candidate in [home.join("bin").join(exe), home.join("jre.bundle/Contents/Home/bin").join(exe)] {
        if candidate.exists() {
            return Ok(candidate.display().to_string());
        }
    }
    Err("Pobrano Javę, ale nie znaleziono pliku wykonywalnego".into())
}

/// Wypakowuje natywne biblioteki LWJGL (dll/so/dylib) z jarow do folderu natives.
fn extract_natives(jars: &[PathBuf], dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    for jar in jars {
        let Ok(file) = std::fs::File::open(jar) else { continue };
        let Ok(mut zip) = zip::ZipArchive::new(file) else { continue };
        for i in 0..zip.len() {
            let Ok(mut entry) = zip.by_index(i) else { continue };
            let name = entry.name().to_string();
            let lower = name.to_lowercase();
            if entry.is_dir() || !(lower.ends_with(".dll") || lower.ends_with(".so") || lower.ends_with(".dylib") || lower.ends_with(".jnilib")) {
                continue;
            }
            let Some(file_name) = Path::new(&name).file_name() else { continue };
            let target = dir.join(file_name);
            if target.exists() {
                continue;
            }
            let mut out = std::fs::File::create(&target).map_err(|e| e.to_string())?;
            std::io::copy(&mut entry, &mut out).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

fn build_command(
    v: &Value,
    account: &Account,
    java: &str,
    classpath: &[PathBuf],
    cfg: &LaunchCfg,
    game_dir: &Path,
    natives: &Path,
) -> Vec<String> {
    let vid = v["_id"].as_str().unwrap_or_default();
    let mc = v["_mc"].as_str().unwrap_or(vid);
    let client_jar = versions().join(mc).join(format!("{mc}.jar"));
    let mut cp: Vec<String> = classpath.iter().map(|p| p.display().to_string()).collect();
    cp.push(client_jar.display().to_string());
    let values: HashMap<&str, String> = HashMap::from([
        ("auth_player_name", account.name.clone()),
        ("auth_uuid", account.uuid.clone()),
        ("auth_access_token", account.token.clone()),
        ("auth_xuid", String::new()),
        ("clientid", String::new()),
        ("user_type", "msa".into()),
        ("user_properties", "{}".into()),
        ("version_name", vid.to_string()),
        ("version_type", v["type"].as_str().unwrap_or("release").to_string()),
        ("game_directory", game_dir.display().to_string()),
        ("assets_root", assets().display().to_string()),
        ("assets_index_name", v["assetIndex"]["id"].as_str().unwrap_or_default().to_string()),
        ("natives_directory", natives.display().to_string()),
        ("library_directory", libs().display().to_string()),
        ("launcher_name", "Zorza Client".into()),
        ("launcher_version", env!("CARGO_PKG_VERSION").into()),
        ("classpath_separator", CP_SEP.into()),
        ("classpath", cp.join(CP_SEP)),
    ]);
    let expand = |args: &Value| -> Vec<String> {
        let mut out = Vec::new();
        for arg in arr(args) {
            let items = if arg.is_object() {
                if !rules_allow(&arg["rules"]) {
                    continue;
                }
                match &arg["value"] {
                    Value::Array(list) => list.clone(),
                    other => vec![other.clone()],
                }
            } else {
                vec![arg]
            };
            for item in items {
                if let Some(s) = item.as_str() {
                    out.push(substitute(s, &values));
                }
            }
        }
        out
    };
    let mut cmd = vec![java.to_string(), format!("-Xmx{}M", cfg.memory_mb)];
    cmd.extend(cfg.jvm_args.split_whitespace().map(str::to_string));
    cmd.extend(expand(&v["arguments"]["jvm"]));
    cmd.push(v["mainClass"].as_str().unwrap_or_default().to_string());
    cmd.extend(expand(&v["arguments"]["game"]));
    cmd
}

/// Zamienia ${nazwa} na wartosci; nieznane zostaja bez zmian.
fn substitute(s: &str, values: &HashMap<&str, String>) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        match after.find('}') {
            Some(end) => {
                let key = &after[..end];
                match values.get(key) {
                    Some(v) => out.push_str(v),
                    None => out.push_str(&rest[start..start + 3 + end]),
                }
                rest = &after[end + 1..];
            }
            None => {
                out.push_str(&rest[start..]);
                rest = "";
            }
        }
    }
    out.push_str(rest);
    out
}

/// Pobiera wszystko, co potrzebne, i zwraca komende uruchomienia (pierwszy element to Java).
pub async fn prepare(cfg: &LaunchCfg, account: &Account, game_dir: &Path, rep: &dyn Reporter) -> Result<Vec<String>, String> {
    let client = crate::net::client()?;
    for d in [libs(), assets(), versions(), game_dir.join("mods")] {
        std::fs::create_dir_all(&d).map_err(|e| format!("Nie można utworzyć {}: {e}", d.display()))?;
    }
    rep.status("Sprawdzam wersję…");
    let v = resolve_version(&client, cfg, rep).await?;
    let mc = v["_mc"].as_str().unwrap_or_default().to_string();
    let loader = v["_loader"].as_array().map(|l| {
        format!(" + {} {}", l[0].as_str().unwrap_or_default(), l[1].as_str().unwrap_or_default())
    });
    rep.status(&format!("Minecraft {mc}{}", loader.unwrap_or_default()));
    let (mut tasks, classpath, natives) = collect_libraries(&v);
    let client_dl = &v["downloads"]["client"];
    tasks.push(Task {
        url: client_dl["url"].as_str().ok_or("Brak pliku gry w opisie wersji")?.to_string(),
        dest: versions().join(&mc).join(format!("{mc}.jar")),
        sha1: client_dl["sha1"].as_str().map(str::to_string),
        size: client_dl["size"].as_u64(),
    });
    rep.status("Pobieram biblioteki i grę…");
    download_all(&client, tasks, 12, |d, t| rep.progress(d, t)).await?;
    if let Some(missing) = classpath.iter().find(|p| !p.exists()) {
        return Err(format!("Brakuje biblioteki {}", missing.display()));
    }
    rep.status("Pobieram zasoby…");
    let asset_tasks = collect_assets(&client, &v).await?;
    download_all(&client, asset_tasks, 16, |d, t| rep.progress(d, t)).await?;
    let java = ensure_java(&client, &v, cfg, rep).await?;
    let natives_dir = versions().join(&mc).join("natives");
    extract_natives(&natives, &natives_dir)?;
    Ok(build_command(&v, account, &java, &classpath, cfg, game_dir, &natives_dir))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maven_paths() {
        assert_eq!(maven_path("org.ow2.asm:asm:9.9"), "org/ow2/asm/asm/9.9/asm-9.9.jar");
        assert_eq!(maven_path("a.b:c:1:natives-windows"), "a/b/c/1/c-1-natives-windows.jar");
        assert_eq!(maven_path("a:b:1@zip"), "a/b/1/b-1.zip");
        assert_eq!(lib_key("a.b:c:1:natives-windows"), "a.b:c:natives-windows");
        assert_eq!(lib_key("a.b:c:2"), "a.b:c");
    }

    #[test]
    fn substitution() {
        let values = HashMap::from([("a", "X".to_string())]);
        assert_eq!(substitute("-D${a}=${b}/${a}", &values), "-DX=${b}/X");
        assert_eq!(substitute("bez", &values), "bez");
        assert_eq!(substitute("${a", &values), "${a");
    }

    #[test]
    fn rules() {
        assert!(rules_allow(&Value::Null));
        assert!(!rules_allow(&json!([{"action": "allow", "os": {"name": "nie-ma-takiego"}}])));
        assert!(!rules_allow(&json!([{"action": "allow", "features": {"is_demo_user": true}}])));
        assert!(rules_allow(&json!([{"action": "allow"}, {"action": "disallow", "os": {"name": "nie-ma-takiego"}}])));
    }
}
