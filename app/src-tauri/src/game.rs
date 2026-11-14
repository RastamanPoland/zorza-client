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
const FORGE_PROMOS: &str = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json";
const FORGE_METADATA: &str = "https://files.minecraftforge.net/net/minecraftforge/forge/maven-metadata.json";
const FORGE_MAVEN: &str = "https://maven.minecraftforge.net/net/minecraftforge/forge";
const NEOFORGE_MAVEN: &str = "https://maven.neoforged.net/releases/net/neoforged/neoforge";
const NEOFORGE_VERSIONS: &str = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge";

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
        manifest["latest"]["release"].as_str().ok_or_else(|| tr!("No latest version in the manifest", "Brak najnowszej wersji w manifeście"))?.to_string()
    } else {
        cfg.game_version.clone()
    };
    let entry = arr(&manifest["versions"])
        .into_iter()
        .find(|v| v["id"] == vid.as_str())
        .ok_or_else(|| tr!("Version {vid} is not in the Mojang manifest", "Nie ma wersji {vid} w manifeście Mojangu"))?;
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
    let mut v: Value = serde_json::from_str(&text).map_err(|e| tr!("Damaged version file: {e}", "Uszkodzony plik wersji: {e}"))?;
    if v["arguments"].is_null() {
        return Err(tr!("This game version is too old (1.19+ is supported)", "Ta wersja gry jest za stara (obsługiwane 1.19+)"));
    }
    v["_id"] = json!(vid);
    v["_mc"] = json!(vid);
    match cfg.loader.as_str() {
        "fabric" | "quilt" => {
            let fabric = cfg.loader == "fabric";
            let (meta, label) = if fabric { (FABRIC_META, "Fabric Loader") } else { (QUILT_META, "Quilt Loader") };
            let loaders = arr(&get_json(client, &format!("{meta}/versions/loader/{}", enc(&vid))).await?);
            if loaders.is_empty() {
                return Err(tr!("{label} does not support {vid} yet. Choose an older game version or another loader.", "{label} nie wspiera jeszcze wersji {vid}. Wybierz starszą wersję gry albo inny loader."));
            }
            let version_of = |l: &Value| l["loader"]["version"].as_str().unwrap_or_default().to_string();
            let pinned = loaders.iter().find(|l| !cfg.loader_version.is_empty() && version_of(l) == cfg.loader_version);
            if !cfg.loader_version.is_empty() && pinned.is_none() {
                rep.status(&tr!("{label} {} does not match {vid}, using the latest", "{label} {} nie pasuje do wersji {vid}, używam najnowszego", cfg.loader_version));
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
            let (version, child) = install_forge_like(client, &cfg.loader, &vid, &cfg.loader_version, &v, cfg, rep).await?;
            let child_id = child["id"].as_str().filter(|s| !s.is_empty()).unwrap_or(&vid).to_string();
            merge_inherited(&mut v, &child);
            v["_id"] = json!(child_id);
            v["_loader"] = json!([cfg.loader, version]);
        }
        _ => {}
    }
    Ok(v)
}

/// 1.21.1 -> "21.1.", 1.21 -> "21.0.", 26.1 -> "26.1.0.", 26.1.2 -> "26.1.2."
fn neoforge_prefix(mc: &str) -> String {
    let p: Vec<&str> = mc.split('.').collect();
    if p.first() == Some(&"1") {
        format!("{}.{}.", p.get(1).unwrap_or(&"0"), p.get(2).unwrap_or(&"0"))
    } else {
        format!("{}.{}.{}.", p.first().unwrap_or(&"0"), p.get(1).unwrap_or(&"0"), p.get(2).unwrap_or(&"0"))
    }
}

#[derive(serde::Serialize)]
pub struct LoaderVersion {
    pub version: String,
    pub stable: bool,
}

/// Dostepne wersje loadera dla danej wersji gry, od najnowszej.
pub async fn loader_versions(client: &reqwest::Client, loader: &str, game_version: &str) -> Result<Vec<LoaderVersion>, String> {
    let mc = if game_version == "latest" {
        get_json(client, MANIFEST_URL).await?["latest"]["release"].as_str().unwrap_or_default().to_string()
    } else {
        game_version.to_string()
    };
    let unstable = |s: &str| s.contains("beta") || s.contains("alpha") || s.contains("pre") || s.contains("rc");
    Ok(match loader {
        "fabric" | "quilt" => {
            let meta = if loader == "fabric" { FABRIC_META } else { QUILT_META };
            arr(&get_json(client, &format!("{meta}/versions/loader/{}", enc(&mc))).await?)
                .iter()
                .map(|l| {
                    let version = l["loader"]["version"].as_str().unwrap_or_default().to_string();
                    let stable = if loader == "fabric" { l["loader"]["stable"].as_bool().unwrap_or(false) } else { !unstable(&version) };
                    LoaderVersion { version, stable }
                })
                .collect()
        }
        "forge" => {
            let prefix = format!("{mc}-");
            let mut list: Vec<LoaderVersion> = arr(&get_json(client, FORGE_METADATA).await?[mc.as_str()])
                .iter()
                .filter_map(|v| v.as_str()?.strip_prefix(&prefix).map(str::to_string))
                .map(|version| LoaderVersion { stable: true, version })
                .collect();
            list.reverse();
            list
        }
        "neoforge" => {
            let prefix = neoforge_prefix(&mc);
            let mut list: Vec<LoaderVersion> = arr(&get_json(client, NEOFORGE_VERSIONS).await?["versions"])
                .iter()
                .filter_map(|v| v.as_str().filter(|s| s.starts_with(&prefix)).map(str::to_string))
                .map(|version| LoaderVersion { stable: !unstable(&version), version })
                .collect();
            list.reverse();
            list
        }
        _ => vec![],
    })
}

async fn forge_like_version(client: &reqwest::Client, loader: &str, mc: &str, pin: &str) -> Result<String, String> {
    if !pin.is_empty() {
        return Ok(pin.to_string());
    }
    if loader == "forge" {
        let promos = get_json(client, FORGE_PROMOS).await?;
        let promos = &promos["promos"];
        return promos[format!("{mc}-recommended").as_str()]
            .as_str()
            .or_else(|| promos[format!("{mc}-latest").as_str()].as_str())
            .map(str::to_string)
            .ok_or_else(|| tr!("Forge has no version for Minecraft {mc} yet", "Forge nie ma jeszcze wersji dla Minecraft {mc}"));
    }
    let list = loader_versions(client, "neoforge", mc).await?;
    list.iter()
        .find(|l| l.stable)
        .or(list.first())
        .map(|l| l.version.clone())
        .ok_or_else(|| tr!("NeoForge has no version for Minecraft {mc} yet", "NeoForge nie ma jeszcze wersji dla Minecraft {mc}"))
}

/// Uruchamia oficjalny instalator Forge/NeoForge bez okna. Zwraca (wersja, json wersji).
async fn install_forge_like(
    client: &reqwest::Client,
    loader: &str,
    mc: &str,
    pin: &str,
    vanilla: &Value,
    cfg: &LaunchCfg,
    rep: &dyn Reporter,
) -> Result<(String, Value), String> {
    let label = if loader == "forge" { "Forge" } else { "NeoForge" };
    let version = forge_like_version(client, loader, mc, pin).await?;
    if !version.chars().all(|c| c.is_ascii_alphanumeric() || ".-+_".contains(c)) {
        return Err(tr!("Invalid {label} version: {version}", "Nieprawidłowa wersja {label}: {version}"));
    }
    let url = if loader == "forge" {
        format!("{FORGE_MAVEN}/{mc}-{version}/forge-{mc}-{version}-installer.jar")
    } else {
        format!("{NEOFORGE_MAVEN}/{version}/neoforge-{version}-installer.jar")
    };
    let installer = root_dir().join("cache").join("installers").join(url.rsplit('/').next().unwrap_or("installer.jar"));
    rep.status(&tr!("Downloading the {label} {version} installer…", "Pobieram instalator {label} {version}…"));
    download(client, &Task { url, dest: installer.clone(), sha1: None, size: None })
        .await
        .map_err(|e| tr!("{e}. Check that {label} {version} exists for Minecraft {mc}.", "{e}. Sprawdź, czy {label} {version} istnieje dla Minecraft {mc}."))?;
    let vid = (|| {
        let mut zip = zip::ZipArchive::new(std::fs::File::open(&installer).ok()?).ok()?;
        let mut text = String::new();
        std::io::Read::read_to_string(&mut zip.by_name("install_profile.json").ok()?, &mut text).ok()?;
        let profile: Value = serde_json::from_str(&text).ok()?;
        profile["version"].as_str().map(str::to_string)
    })();
    let Some(vid) = vid else {
        let _ = std::fs::remove_file(&installer);
        return Err(tr!("The {label} {version} installer is damaged, try again", "Instalator {label} {version} jest uszkodzony, spróbuj ponownie"));
    };
    if vid.is_empty() || vid.contains('/') || vid.contains('\\') || vid.starts_with('.') {
        return Err(tr!("Unexpected version name in the {label} installer: {vid}", "Nieoczekiwana nazwa wersji w instalatorze {label}: {vid}"));
    }
    let vjson = versions().join(&vid).join(format!("{vid}.json"));
    if !vjson.exists() {
        let profiles_file = root_dir().join("launcher_profiles.json"); // instalator nie dziala bez tego pliku
        if !profiles_file.exists() {
            std::fs::write(&profiles_file, r#"{"profiles":{}}"#).map_err(|e| e.to_string())?;
        }
        let java = ensure_java(client, vanilla, cfg, rep).await?;
        rep.status(&tr!("Installing {label} {version}. The first time this can take a few minutes…", "Instaluję {label} {version}. Za pierwszym razem może to potrwać kilka minut…"));
        let root = root_dir();
        let dir = installer.parent().map(Path::to_path_buf).unwrap_or_else(|| root.clone());
        let installer_path = installer.clone();
        let output = tauri::async_runtime::spawn_blocking(move || {
            let mut cmd = std::process::Command::new(&java);
            cmd.arg("-jar").arg(&installer_path).arg("--installClient").arg(&root).current_dir(dir);
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                cmd.creation_flags(0x0800_0000);
            }
            cmd.output()
        })
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| tr!("Could not start the {label} installer: {e}", "Nie udało się uruchomić instalatora {label}: {e}"))?;
        if !output.status.success() || !vjson.exists() {
            let out = String::from_utf8_lossy(&output.stdout);
            let err = String::from_utf8_lossy(&output.stderr);
            let tail = |t: &str, n: usize| t.chars().rev().take(n).collect::<String>().chars().rev().collect::<String>();
            return Err(tr!(
                "The {label} installer failed (code {:?}):\n{}{}",
                "Instalator {label} zakończył się błędem (kod {:?}):\n{}{}",
                output.status.code(),
                tail(&out, 1200),
                tail(&err, 600)
            ));
        }
    }
    let text = std::fs::read_to_string(&vjson).map_err(|e| e.to_string())?;
    let child: Value = serde_json::from_str(&text).map_err(|e| tr!("Damaged {label} version file: {e}", "Uszkodzony plik wersji {label}: {e}"))?;
    Ok((version, child))
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
    let id = idx["id"].as_str().ok_or_else(|| tr!("No asset index", "Brak indeksu zasobów"))?;
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
        return Err(tr!("Mojang has no Java {major} for this system. Install it and enter its path in Settings.", "Mojang nie ma Javy {major} dla tego systemu. Zainstaluj ją i wpisz ścieżkę w Ustawieniach."));
    };
    let all = get_json(client, JAVA_RUNTIMES_URL).await?;
    let entries = arr(&all[key][component.as_str()]);
    let Some(entry) = entries.first() else {
        return Err(tr!("Mojang has no Java {major} for this system. Install it and enter its path in Settings.", "Mojang nie ma Javy {major} dla tego systemu. Zainstaluj ją i wpisz ścieżkę w Ustawieniach."));
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
    Err(tr!("Java was downloaded, but its executable was not found", "Pobrano Javę, ale nie znaleziono pliku wykonywalnego"))
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
    let client_jar = versions().join(vid).join(format!("{vid}.jar"));
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
        std::fs::create_dir_all(&d).map_err(|e| tr!("Cannot create {}: {e}", "Nie można utworzyć {}: {e}", d.display()))?;
    }
    rep.status(&tr!("Checking the version…", "Sprawdzam wersję…"));
    let v = resolve_version(&client, cfg, rep).await?;
    let mc = v["_mc"].as_str().unwrap_or_default().to_string();
    let loader = v["_loader"].as_array().map(|l| {
        format!(" + {} {}", l[0].as_str().unwrap_or_default(), l[1].as_str().unwrap_or_default())
    });
    rep.status(&format!("Minecraft {mc}{}", loader.unwrap_or_default()));
    let (mut tasks, classpath, natives) = collect_libraries(&v);
    let client_dl = &v["downloads"]["client"];
    tasks.push(Task {
        url: client_dl["url"].as_str().ok_or_else(|| tr!("No game file in the version description", "Brak pliku gry w opisie wersji"))?.to_string(),
        dest: versions().join(&mc).join(format!("{mc}.jar")),
        sha1: client_dl["sha1"].as_str().map(str::to_string),
        size: client_dl["size"].as_u64(),
    });
    rep.status(&tr!("Downloading libraries and the game…", "Pobieram biblioteki i grę…"));
    download_all(&client, tasks, 12, |d, t| rep.progress(d, t)).await?;
    if let Some(missing) = classpath.iter().find(|p| !p.exists()) {
        return Err(tr!("Missing library {}", "Brakuje biblioteki {}", missing.display()));
    }
    let vid = v["_id"].as_str().unwrap_or_default().to_string();
    let jar = versions().join(&vid).join(format!("{vid}.jar"));
    if vid != mc && !jar.exists() {
        // jak oficjalny launcher: gra pod nazwa wersji z loaderem
        std::fs::copy(versions().join(&mc).join(format!("{mc}.jar")), &jar).map_err(|e| e.to_string())?;
    }
    rep.status(&tr!("Downloading assets…", "Pobieram zasoby…"));
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
    fn neoforge_prefixes() {
        assert_eq!(neoforge_prefix("1.21.1"), "21.1.");
        assert_eq!(neoforge_prefix("1.21"), "21.0.");
        assert_eq!(neoforge_prefix("26.1"), "26.1.0.");
        assert_eq!(neoforge_prefix("26.1.2"), "26.1.2.");
    }

    #[test]
    fn rules() {
        assert!(rules_allow(&Value::Null));
        assert!(!rules_allow(&json!([{"action": "allow", "os": {"name": "nie-ma-takiego"}}])));
        assert!(!rules_allow(&json!([{"action": "allow", "features": {"is_demo_user": true}}])));
        assert!(rules_allow(&json!([{"action": "allow"}, {"action": "disallow", "os": {"name": "nie-ma-takiego"}}])));
    }
}
