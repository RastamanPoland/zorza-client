//! Mody, modpacki, paczki zasobow i danych z Modrinth oraz CurseForge.
//! Indeksy zainstalowanych plikow sa wspolne z wersja w Pythonie.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashSet, VecDeque};
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use crate::game::Reporter;
use crate::net::{download, download_all, get_json, Task};
use crate::{profiles, root_dir};

const MODRINTH: &str = "https://api.modrinth.com/v2";
const CURSEFORGE: &str = "https://api.curseforge.com/v1";

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Hit {
    pub source: String,
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub downloads: u64,
    #[serde(default)]
    pub author: String,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub icon: String,
}

#[derive(Serialize)]
pub struct Details {
    pub description: String,
    pub categories: Vec<String>,
    pub updated: String,
    pub license: String,
    pub followers: Option<u64>,
    pub links: Vec<(String, String)>,
}

#[derive(Serialize, Clone)]
pub struct Blocked {
    pub name: String,
    pub file: String,
    pub folder: String,
    pub url: String,
}

#[derive(Serialize)]
pub struct ModpackInfo {
    pub name: String,
    pub game_version: String,
    pub loader: String,
    pub loader_version: String,
    pub version: String,
    pub files: usize,
    pub blocked: Vec<Blocked>,
    pub missing: usize,
}

#[derive(Serialize)]
pub struct InstalledFile {
    pub file: String,
    pub base: String,
    pub enabled: bool,
    pub title: String,
    pub source: String,
    pub id: String,
}

// ------------------------------------------------------------------ pomocnicze

/// Wlasny klucz z ustawien albo klucz wbudowany podczas kompilacji (ZORZA_CURSEFORGE_KEY).
pub fn cf_key() -> String {
    let own = profiles::load_config()["curseforge_api_key"].as_str().unwrap_or_default().trim().to_string();
    if !own.is_empty() {
        return own;
    }
    option_env!("ZORZA_CURSEFORGE_KEY").unwrap_or_default().trim().to_string()
}

fn mr_loaders(loader: &str) -> Vec<&'static str> {
    match loader {
        "fabric" => vec!["fabric"],
        "quilt" => vec!["quilt", "fabric"], // Quilt uruchamia tez mody Fabric
        "forge" => vec!["forge"],
        "neoforge" => vec!["neoforge"],
        _ => vec![],
    }
}

fn cf_loader(loader: &str) -> Option<u32> {
    match loader {
        "fabric" | "quilt" => Some(4),
        "forge" => Some(1),
        "neoforge" => Some(6),
        _ => None,
    }
}

pub fn loader_label(loader: &str) -> &'static str {
    match loader {
        "fabric" => "Fabric",
        "quilt" => "Quilt",
        "forge" => "Forge",
        "neoforge" => "NeoForge",
        _ => "Vanilla",
    }
}

fn enc(s: &str) -> String {
    s.chars()
        .map(|c| if c.is_ascii_alphanumeric() || "-._~".contains(c) { c.to_string() } else {
            let mut buf = [0u8; 4];
            c.encode_utf8(&mut buf).bytes().map(|b| format!("%{b:02X}")).collect()
        })
        .collect()
}

fn query(params: &[(&str, String)]) -> String {
    params.iter().map(|(k, v)| format!("{k}={}", enc(v))).collect::<Vec<_>>().join("&")
}

fn s(v: &Value) -> String {
    match v {
        Value::String(x) => x.clone(),
        Value::Number(n) => n.to_string(),
        _ => String::new(),
    }
}

fn arr(v: &Value) -> Vec<Value> {
    v.as_array().cloned().unwrap_or_default()
}

async fn cf_send(req: reqwest::RequestBuilder, key: &str) -> Result<Value, String> {
    if key.is_empty() {
        return Err(tr!("No CurseForge API key. Enter your own in Settings or use Modrinth.", "Brak klucza API CurseForge. Wpisz własny w Ustawieniach albo użyj Modrinth."));
    }
    let resp = req.header("x-api-key", key).header("Accept", "application/json").send().await.map_err(|e| e.to_string())?;
    match resp.status().as_u16() {
        401 | 403 => Err(tr!("CurseForge rejected the API key (check it in Settings)", "CurseForge odrzucił klucz API (sprawdź go w Ustawieniach)")),
        _ => resp.error_for_status().map_err(|e| e.to_string())?.json().await.map_err(|e| e.to_string()),
    }
}

async fn cf_get(client: &reqwest::Client, path: &str) -> Result<Value, String> {
    cf_send(client.get(format!("{CURSEFORGE}{path}")), &cf_key()).await
}

async fn cf_post(client: &reqwest::Client, path: &str, body: Value) -> Result<Value, String> {
    cf_send(client.post(format!("{CURSEFORGE}{path}")).json(&body), &cf_key()).await
}

fn cf_sha1(f: &Value) -> Option<String> {
    arr(&f["hashes"]).iter().find(|h| h["algo"].as_u64() == Some(1)).map(|h| s(&h["value"]))
}

/// Nazwa pliku bez katalogow, z dozwolonym rozszerzeniem.
fn safe_file_name(name: &str, exts: &[&str]) -> Result<String, String> {
    let name = name.replace('\\', "/");
    let base = name.rsplit('/').next().unwrap_or_default().to_string();
    let lower = base.to_lowercase();
    if base.is_empty() || base.starts_with('.') || !exts.iter().any(|e| lower.ends_with(e)) {
        return Err(tr!("Unexpected file name: {base}", "Nieoczekiwana nazwa pliku: {base}"));
    }
    Ok(base)
}

/// Sciezka z paczki, ktora nie moze wyjsc poza folder profilu.
pub fn safe_join(base: &Path, rel: &str) -> Result<PathBuf, String> {
    let rel = rel.replace('\\', "/");
    let bad = || tr!("Unsafe path in pack: {rel}", "Niebezpieczna ścieżka w paczce: {rel}");
    if rel.starts_with('/') || rel.len() > 1 && rel.as_bytes()[1] == b':' {
        return Err(bad());
    }
    let mut out = base.to_path_buf();
    let mut any = false;
    for part in rel.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." || part.contains(':') {
            return Err(bad());
        }
        match Path::new(part).components().next() {
            Some(Component::Normal(_)) => out.push(part),
            _ => return Err(bad()),
        }
        any = true;
    }
    if !any {
        return Err(bad());
    }
    Ok(out)
}

fn load_index(path: &Path) -> serde_json::Map<String, Value> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn save_index(path: &Path, index: &serde_json::Map<String, Value>) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(path, serde_json::to_string_pretty(index).unwrap_or_default()).map_err(|e| e.to_string())
}

pub fn mods_index(game_dir: &Path) -> PathBuf {
    game_dir.join("zorza_mods.json")
}
pub fn resourcepack_index(game_dir: &Path) -> PathBuf {
    game_dir.join("zorza_resourcepacks.json")
}
pub fn datapack_index(world: &Path) -> PathBuf {
    world.join("zorza_datapacks.json")
}

/// Pobiera plik, usuwa starsza wersje tego samego projektu i zapisuje wpis w indeksie.
#[allow(clippy::too_many_arguments)]
async fn store_file(
    client: &reqwest::Client,
    target_dir: &Path,
    index_path: &Path,
    url: &str,
    filename: &str,
    exts: &[&str],
    sha1: Option<String>,
    size: Option<u64>,
    source: &str,
    project_id: &str,
    title: &str,
    version: &str,
) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err(tr!("Rejected a download address that does not use HTTPS", "Odrzucono adres pobierania, który nie używa HTTPS"));
    }
    let name = safe_file_name(filename, exts)?;
    let mut index = load_index(index_path);
    let old: Vec<String> = index
        .iter()
        .filter(|(f, i)| s(&i["source"]) == source && s(&i["id"]) == project_id && **f != name)
        .map(|(f, _)| f.clone())
        .collect();
    for f in old {
        let _ = std::fs::remove_file(target_dir.join(&f));
        let _ = std::fs::remove_file(target_dir.join(format!("{f}.disabled")));
        index.remove(&f);
    }
    let _ = std::fs::remove_file(target_dir.join(format!("{name}.disabled")));
    download(client, &Task { url: url.into(), dest: target_dir.join(&name), sha1, size }).await?;
    index.insert(name, json!({"source": source, "id": project_id, "title": title, "version": version}));
    save_index(index_path, &index)
}

// ------------------------------------------------------------------ wyszukiwanie

fn modrinth_hit(h: &Value, kind: &str) -> Hit {
    let id = s(&h["project_id"]);
    let slug = h["slug"].as_str().map(str::to_string).unwrap_or_else(|| id.clone());
    let page = match kind {
        "modpack" => "modpack",
        "resourcepack" => "resourcepack",
        "datapack" => "datapack",
        _ => "mod",
    };
    Hit {
        title: h["title"].as_str().map(str::to_string).unwrap_or_else(|| id.clone()),
        description: s(&h["description"]),
        downloads: h["downloads"].as_u64().unwrap_or(0),
        author: s(&h["author"]),
        url: format!("https://modrinth.com/{page}/{slug}"),
        icon: s(&h["icon_url"]),
        source: "modrinth".into(),
        id,
    }
}

fn curseforge_hit(m: &Value) -> Hit {
    Hit {
        source: "curseforge".into(),
        id: s(&m["id"]),
        title: s(&m["name"]),
        description: s(&m["summary"]),
        downloads: m["downloadCount"].as_f64().unwrap_or(0.0) as u64,
        author: s(&m["authors"][0]["name"]),
        url: s(&m["links"]["websiteUrl"]),
        icon: s(&m["logo"]["thumbnailUrl"]),
    }
}

fn cf_class(kind: &str) -> u32 {
    match kind {
        "modpack" => 4471,
        "resourcepack" => 12,
        "datapack" => 6945,
        _ => 6,
    }
}

pub async fn search(
    kind: &str,
    source: &str,
    text: &str,
    game_version: &str,
    loader: &str,
    sort: &str,
    offset: usize,
) -> Result<Vec<Hit>, String> {
    let client = crate::net::client()?;
    let version = if kind == "modpack" { "" } else { game_version };
    if kind == "mod" && mr_loaders(loader).is_empty() {
        return Err(tr!("This profile has no mod loader. Choose Fabric, Quilt, Forge or NeoForge in the profile editor.", "Ten profil nie ma loadera modów. Wybierz Fabric, Quilt, Forge albo NeoForge w edycji profilu."));
    }
    if source == "modrinth" {
        let mut facets: Vec<Vec<String>> = match kind {
            "mod" => vec![
                vec!["project_type:mod".into()],
                mr_loaders(loader).iter().map(|l| format!("categories:{l}")).collect(),
            ],
            "modpack" => vec![vec!["project_type:modpack".into()]],
            "resourcepack" => vec![vec!["project_type:resourcepack".into()]],
            _ => vec![vec!["categories:datapack".into()]],
        };
        if !version.is_empty() {
            facets.push(vec![format!("versions:{version}")]);
        }
        let index = if !sort.is_empty() && !(text.is_empty() && sort == "relevance") {
            sort.to_string()
        } else if text.is_empty() {
            "downloads".into()
        } else {
            "relevance".into()
        };
        let mut params = vec![
            ("limit", "30".to_string()),
            ("offset", offset.to_string()),
            ("facets", serde_json::to_string(&facets).unwrap()),
            ("index", index),
        ];
        if !text.is_empty() {
            params.push(("query", text.to_string()));
        }
        let v = get_json(&client, &format!("{MODRINTH}/search?{}", query(&params))).await?;
        return Ok(arr(&v["hits"]).iter().map(|h| modrinth_hit(h, kind)).collect());
    }
    let mut params = vec![
        ("gameId", "432".to_string()),
        ("classId", cf_class(kind).to_string()),
        ("pageSize", "30".to_string()),
        ("index", offset.to_string()),
    ];
    if kind == "mod" {
        if let Some(l) = cf_loader(loader) {
            params.push(("modLoaderType", l.to_string()));
        }
    }
    if !version.is_empty() {
        params.push(("gameVersion", version.to_string()));
    }
    if text.is_empty() {
        params.push(("sortField", "2".into()));
        params.push(("sortOrder", "desc".into()));
    } else {
        params.push(("searchFilter", text.to_string()));
    }
    let v = cf_get(&client, &format!("/mods/search?{}", query(&params))).await?;
    Ok(arr(&v["data"]).iter().map(curseforge_hit).collect())
}

pub async fn details(hit: &Hit) -> Result<Details, String> {
    let client = crate::net::client()?;
    let https = |l: Vec<(String, String)>| -> Vec<(String, String)> {
        l.into_iter().filter(|(_, u)| u.starts_with("https://")).collect()
    };
    if hit.source == "modrinth" {
        let p = get_json(&client, &format!("{MODRINTH}/project/{}", enc(&hit.id))).await?;
        let skip = ["fabric", "quilt", "forge", "neoforge", "datapack", "minecraft"];
        return Ok(Details {
            description: p["body"].as_str().map(str::to_string).unwrap_or_else(|| s(&p["description"])),
            categories: arr(&p["categories"]).iter().map(s).filter(|c| !skip.contains(&c.as_str())).collect(),
            updated: s(&p["updated"]).chars().take(10).collect(),
            license: p["license"]["name"].as_str().filter(|x| !x.is_empty()).map(str::to_string).unwrap_or_else(|| s(&p["license"]["id"])),
            followers: p["followers"].as_u64(),
            links: https(vec![
                (tr!("Project page", "Strona projektu"), hit.url.clone()),
                (tr!("Source code", "Kod źródłowy"), s(&p["source_url"])),
                (tr!("Report a bug", "Zgłoś błąd"), s(&p["issues_url"])),
                ("Wiki".to_string(), s(&p["wiki_url"])),
                ("Discord".to_string(), s(&p["discord_url"])),
            ]),
        });
    }
    let id: u64 = hit.id.parse().map_err(|_| tr!("Invalid project ID", "Nieprawidłowe ID projektu"))?;
    let data = cf_get(&client, &format!("/mods/{id}")).await?["data"].clone();
    let description = match cf_get(&client, &format!("/mods/{id}/description")).await {
        Ok(v) => s(&v["data"]),
        Err(_) => s(&data["summary"]),
    };
    let links = &data["links"];
    let site = links["websiteUrl"].as_str().filter(|x| !x.is_empty()).map(str::to_string).unwrap_or_else(|| hit.url.clone());
    Ok(Details {
        description,
        categories: arr(&data["categories"]).iter().map(|c| s(&c["name"])).filter(|c| !c.is_empty()).collect(),
        updated: s(&data["dateModified"]).chars().take(10).collect(),
        license: String::new(),
        followers: None,
        links: https(vec![
            (tr!("Project page", "Strona projektu"), site),
            (tr!("Source code", "Kod źródłowy"), s(&links["sourceUrl"])),
            (tr!("Report a bug", "Zgłoś błąd"), s(&links["issuesUrl"])),
            ("Wiki".to_string(), s(&links["wikiUrl"])),
        ]),
    })
}

// ------------------------------------------------------------------ instalacja

async fn modrinth_pick(client: &reqwest::Client, id: &str, loaders: &[&str], game_version: &str) -> Result<Option<Value>, String> {
    let mut params = vec![("loaders", serde_json::to_string(loaders).unwrap())];
    if !game_version.is_empty() {
        params.push(("game_versions", serde_json::to_string(&[game_version]).unwrap()));
    }
    let v = get_json(client, &format!("{MODRINTH}/project/{}/version?{}", enc(id), query(&params))).await?;
    Ok(arr(&v).into_iter().next())
}

async fn cf_pick(client: &reqwest::Client, id: &str, game_version: &str, loader: Option<u32>) -> Result<Option<Value>, String> {
    let id: u64 = id.parse().map_err(|_| tr!("Invalid project ID", "Nieprawidłowe ID projektu"))?;
    let mut params = vec![("pageSize", "50".to_string())];
    if !game_version.is_empty() {
        params.push(("gameVersion", game_version.to_string()));
    }
    if let Some(l) = loader {
        params.push(("modLoaderType", l.to_string()));
    }
    let mut files = arr(&cf_get(client, &format!("/mods/{id}/files?{}", query(&params))).await?["data"]);
    files.sort_by(|a, b| s(&b["fileDate"]).cmp(&s(&a["fileDate"])));
    Ok(files.into_iter().next())
}

fn primary_file(version: &Value, title: &str) -> Result<Value, String> {
    let files = arr(&version["files"]);
    files
        .iter()
        .find(|f| f["primary"].as_bool() == Some(true))
        .or(files.first())
        .cloned()
        .ok_or_else(|| tr!("“{title}” has no file to download", "„{title}” nie ma pliku do pobrania"))
}

/// Instaluje mod razem z wymaganymi zaleznosciami. Zwraca nazwy zainstalowanych modow.
pub async fn install_mod(hit: &Hit, game_version: &str, game_dir: &Path, loader: &str) -> Result<Vec<String>, String> {
    if mr_loaders(loader).is_empty() {
        return Err(tr!("This profile has no mod loader. Choose Fabric, Quilt, Forge or NeoForge.", "Ten profil nie ma loadera modów. Wybierz Fabric, Quilt, Forge albo NeoForge."));
    }
    let client = crate::net::client()?;
    let mods_dir = game_dir.join("mods");
    let index = mods_index(game_dir);
    let mut names = Vec::new();
    let mut seen = HashSet::new();
    let mut queue = VecDeque::from([(hit.id.clone(), Some(hit.title.clone()))]);
    let label = loader_label(loader);
    while let Some((id, title)) = queue.pop_front() {
        if !seen.insert(id.clone()) {
            continue;
        }
        let result: Result<(), String> = async {
            if hit.source == "modrinth" {
                let title = match title {
                    Some(t) => t,
                    None => s(&get_json(&client, &format!("{MODRINTH}/project/{}", enc(&id))).await?["title"]),
                };
                let ver = modrinth_pick(&client, &id, &mr_loaders(loader), game_version)
                    .await?
                    .ok_or_else(|| tr!("“{title}” has no version for Minecraft {game_version} with {label}", "„{title}” nie ma wersji dla Minecraft {game_version} z {label}"))?;
                let f = primary_file(&ver, &title)?;
                store_file(&client, &mods_dir, &index, &s(&f["url"]), &s(&f["filename"]), &[".jar"],
                    f["hashes"]["sha1"].as_str().map(str::to_string), f["size"].as_u64(),
                    "modrinth", &id, &title, &s(&ver["version_number"])).await?;
                names.push(title);
                for dep in arr(&ver["dependencies"]) {
                    if dep["dependency_type"] != "required" {
                        continue;
                    }
                    let mut dep_id = s(&dep["project_id"]);
                    if dep_id.is_empty() && !s(&dep["version_id"]).is_empty() {
                        dep_id = s(&get_json(&client, &format!("{MODRINTH}/version/{}", enc(&s(&dep["version_id"])))).await?["project_id"]);
                    }
                    if !dep_id.is_empty() {
                        queue.push_back((dep_id, None));
                    }
                }
            } else {
                let title = match title {
                    Some(t) => t,
                    None => s(&cf_get(&client, &format!("/mods/{id}")).await?["data"]["name"]),
                };
                let f = cf_pick(&client, &id, game_version, cf_loader(loader))
                    .await?
                    .ok_or_else(|| tr!("“{title}” has no version for Minecraft {game_version} with {label}", "„{title}” nie ma wersji dla Minecraft {game_version} z {label}"))?;
                let url = s(&f["downloadUrl"]);
                if url.is_empty() {
                    return Err(tr!("The author of “{title}” does not allow downloads through third-party launchers. Download the file from CurseForge and put it in the mods folder.", "Autor „{title}” nie pozwala pobierać moda przez zewnętrzne launchery. Pobierz plik ze strony CurseForge i wrzuć go do folderu mods."));
                }
                store_file(&client, &mods_dir, &index, &url, &s(&f["fileName"]), &[".jar"], cf_sha1(&f),
                    f["fileLength"].as_u64(), "curseforge", &id, &title, &s(&f["displayName"])).await?;
                names.push(title);
                for dep in arr(&f["dependencies"]) {
                    if dep["relationType"].as_u64() == Some(3) {
                        let dep_id = s(&dep["modId"]);
                        if !dep_id.is_empty() {
                            queue.push_back((dep_id, None));
                        }
                    }
                }
            }
            Ok(())
        }
        .await;
        if let Err(e) = result {
            return Err(if names.is_empty() { e } else { tr!("{e} (already downloaded: {})", "{e} (pobrano już: {})", names.join(", ")) });
        }
    }
    Ok(names)
}

/// Paczka zasobow albo danych: jeden plik .zip.
pub async fn install_pack(hit: &Hit, kind: &str, game_version: &str, target_dir: &Path, index_path: &Path) -> Result<Vec<String>, String> {
    let client = crate::net::client()?;
    let title = hit.title.clone();
    if hit.source == "modrinth" {
        let loaders: &[&str] = if kind == "datapack" { &["datapack"] } else { &["minecraft"] };
        let ver = modrinth_pick(&client, &hit.id, loaders, game_version)
            .await?
            .ok_or_else(|| tr!("“{title}” has no version for Minecraft {game_version}", "„{title}” nie ma wersji dla Minecraft {game_version}"))?;
        let f = primary_file(&ver, &title)?;
        store_file(&client, target_dir, index_path, &s(&f["url"]), &s(&f["filename"]), &[".zip"],
            f["hashes"]["sha1"].as_str().map(str::to_string), f["size"].as_u64(),
            "modrinth", &hit.id, &title, &s(&ver["version_number"])).await?;
    } else {
        let f = cf_pick(&client, &hit.id, game_version, None)
            .await?
            .ok_or_else(|| tr!("“{title}” has no version for Minecraft {game_version}", "„{title}” nie ma wersji dla Minecraft {game_version}"))?;
        let url = s(&f["downloadUrl"]);
        if url.is_empty() {
            return Err(tr!("The author of “{title}” does not allow downloads through third-party launchers. Download the file from CurseForge.", "Autor „{title}” nie pozwala pobierać przez zewnętrzne launchery. Pobierz plik ze strony CurseForge."));
        }
        store_file(&client, target_dir, index_path, &url, &s(&f["fileName"]), &[".zip"], cf_sha1(&f),
            f["fileLength"].as_u64(), "curseforge", &hit.id, &title, &s(&f["displayName"])).await?;
    }
    Ok(vec![title])
}

// ------------------------------------------------------------------ modpacki

fn read_zip_entry(zip: &mut zip::ZipArchive<std::fs::File>, name: &str) -> Option<String> {
    let mut entry = zip.by_name(name).ok()?;
    let mut text = String::new();
    entry.read_to_string(&mut text).ok()?;
    Some(text)
}

fn extract_overrides(zip: &mut zip::ZipArchive<std::fs::File>, prefix: &str, game_dir: &Path) -> Result<usize, String> {
    let prefix = format!("{}/", prefix.trim_matches('/'));
    let mut count = 0;
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).map_err(|e| e.to_string())?;
        let name = entry.name().replace('\\', "/");
        if entry.is_dir() || !name.starts_with(&prefix) || name == prefix {
            continue;
        }
        let target = safe_join(game_dir, &name[prefix.len()..])?;
        if let Some(dir) = target.parent() {
            std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        }
        let mut out = std::fs::File::create(&target).map_err(|e| e.to_string())?;
        std::io::copy(&mut entry, &mut out).map_err(|e| e.to_string())?;
        count += 1;
    }
    Ok(count)
}

pub async fn install_modpack_file(path: &Path, game_dir: &Path, rep: &dyn Reporter) -> Result<ModpackInfo, String> {
    let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut zip = zip::ZipArchive::new(file).map_err(|_| tr!("This is not a valid modpack file", "To nie jest prawidłowy plik modpacka"))?;
    std::fs::create_dir_all(game_dir).map_err(|e| e.to_string())?;
    let client = crate::net::client()?;
    if let Some(text) = read_zip_entry(&mut zip, "modrinth.index.json") {
        let index: Value = serde_json::from_str(&text).map_err(|e| tr!("Damaged modrinth.index.json: {e}", "Uszkodzony modrinth.index.json: {e}"))?;
        let deps = &index["dependencies"];
        let mc = s(&deps["minecraft"]);
        if mc.is_empty() {
            return Err(tr!("The modpack does not specify a Minecraft version", "Modpack nie podaje wersji Minecrafta"));
        }
        let (mut loader, mut loader_version) = ("vanilla".to_string(), String::new());
        for (key, name) in [("fabric-loader", "fabric"), ("quilt-loader", "quilt"), ("forge", "forge"), ("neoforge", "neoforge")] {
            if !s(&deps[key]).is_empty() {
                loader = name.into();
                loader_version = s(&deps[key]);
                break;
            }
        }
        let mut tasks = Vec::new();
        for f in arr(&index["files"]) {
            if f["env"]["client"] == "unsupported" {
                continue;
            }
            let url = arr(&f["downloads"]).iter().map(s).find(|u| u.starts_with("https://"))
                .ok_or_else(|| tr!("No safe download address for {}", "Brak bezpiecznego adresu pobierania dla {}", s(&f["path"])))?;
            tasks.push(Task {
                url,
                dest: safe_join(game_dir, &s(&f["path"]))?,
                sha1: f["hashes"]["sha1"].as_str().map(str::to_string),
                size: f["fileSize"].as_u64(),
            });
        }
        let files = tasks.len();
        rep.status(&tr!("Downloading {files} modpack files…", "Pobieram {files} plików modpacka…"));
        download_all(&client, tasks, 12, |d, t| rep.progress(d, t)).await?;
        extract_overrides(&mut zip, "overrides", game_dir)?;
        extract_overrides(&mut zip, "client-overrides", game_dir)?;
        return Ok(ModpackInfo {
            name: index["name"].as_str().filter(|x| !x.is_empty()).unwrap_or("Modpack").to_string(),
            game_version: mc,
            loader,
            loader_version,
            version: s(&index["versionId"]),
            files,
            blocked: vec![],
            missing: 0,
        });
    }
    let Some(text) = read_zip_entry(&mut zip, "manifest.json") else {
        return Err(tr!("The file contains neither modrinth.index.json nor manifest.json", "Plik nie zawiera ani modrinth.index.json, ani manifest.json"));
    };
    let m: Value = serde_json::from_str(&text).map_err(|e| tr!("Damaged manifest.json: {e}", "Uszkodzony manifest.json: {e}"))?;
    let mc = s(&m["minecraft"]["version"]);
    if mc.is_empty() {
        return Err(tr!("The modpack does not specify a Minecraft version", "Modpack nie podaje wersji Minecrafta"));
    }
    let entries = arr(&m["minecraft"]["modLoaders"]);
    let primary = entries.iter().find(|x| x["primary"].as_bool() == Some(true)).or(entries.first()).cloned().unwrap_or(Value::Null);
    let loader_id = s(&primary["id"]);
    let (mut loader, mut loader_version) = ("vanilla".to_string(), String::new());
    if !loader_id.is_empty() {
        let (name, ver) = loader_id.split_once('-').unwrap_or((loader_id.as_str(), ""));
        if mr_loaders(name).is_empty() {
            return Err(tr!("This modpack needs the {loader_id} loader, which Zorza does not support", "Ten modpack wymaga loadera {loader_id}, którego Zorza nie obsługuje"));
        }
        loader = name.to_string();
        loader_version = ver.to_string();
    }
    let file_ids: Vec<u64> = arr(&m["files"])
        .iter()
        .filter(|f| f["required"].as_bool() != Some(false))
        .filter_map(|f| f["fileID"].as_u64())
        .collect();
    let files = if file_ids.is_empty() { vec![] } else { arr(&cf_post(&client, "/mods/files", json!({"fileIds": file_ids})).await?["data"]) };
    let mut mod_ids: Vec<u64> = files.iter().filter_map(|f| f["modId"].as_u64()).collect();
    mod_ids.sort_unstable();
    mod_ids.dedup();
    let mods = if mod_ids.is_empty() { vec![] } else { arr(&cf_post(&client, "/mods", json!({"modIds": mod_ids})).await?["data"]) };
    let mut tasks = Vec::new();
    let mut blocked = Vec::new();
    for f in &files {
        let project = mods.iter().find(|x| x["id"] == f["modId"]).cloned().unwrap_or(Value::Null);
        let name = safe_file_name(&s(&f["fileName"]), &[".jar", ".zip"])?;
        let folder = match project["classId"].as_u64() {
            Some(12) => "resourcepacks",
            Some(6552) => "shaderpacks",
            _ if name.to_lowercase().ends_with(".jar") => "mods",
            _ => "resourcepacks",
        };
        let url = s(&f["downloadUrl"]);
        if !url.starts_with("https://") {
            blocked.push(Blocked {
                name: project["name"].as_str().map(str::to_string).unwrap_or_else(|| name.clone()),
                file: name,
                folder: folder.into(),
                url: s(&project["links"]["websiteUrl"]),
            });
            continue;
        }
        tasks.push(Task { url, dest: game_dir.join(folder).join(&name), sha1: cf_sha1(f), size: f["fileLength"].as_u64() });
    }
    let count = tasks.len();
    rep.status(&tr!("Downloading {count} modpack files…", "Pobieram {count} plików modpacka…"));
    download_all(&client, tasks, 12, |d, t| rep.progress(d, t)).await?;
    let overrides = m["overrides"].as_str().filter(|x| !x.is_empty()).unwrap_or("overrides").to_string();
    extract_overrides(&mut zip, &overrides, game_dir)?;
    Ok(ModpackInfo {
        name: m["name"].as_str().filter(|x| !x.is_empty()).unwrap_or("Modpack").to_string(),
        game_version: mc,
        loader,
        loader_version,
        version: s(&m["version"]),
        files: count,
        blocked,
        missing: file_ids.len().saturating_sub(files.len()),
    })
}

pub async fn install_modpack(hit: &Hit, game_dir: &Path, rep: &dyn Reporter) -> Result<ModpackInfo, String> {
    let client = crate::net::client()?;
    let cache = root_dir().join("cache");
    let title = &hit.title;
    let (url, name, sha1, size) = if hit.source == "modrinth" {
        let ver = modrinth_pick(&client, &hit.id, &["fabric", "quilt", "forge", "neoforge"], "")
            .await?
            .ok_or_else(|| tr!("“{title}” has no version to download", "„{title}” nie ma wersji do pobrania"))?;
        let f = primary_file(&ver, title)?;
        (s(&f["url"]), safe_file_name(&s(&f["filename"]), &[".mrpack"])?, f["hashes"]["sha1"].as_str().map(str::to_string), f["size"].as_u64())
    } else {
        let f = cf_pick(&client, &hit.id, "", None).await?.ok_or_else(|| tr!("“{title}” has no version to download", "„{title}” nie ma wersji do pobrania"))?;
        if s(&f["downloadUrl"]).is_empty() {
            return Err(tr!("The author of “{title}” does not allow modpack downloads through third-party launchers.", "Autor „{title}” nie pozwala pobierać modpacka przez zewnętrzne launchery."));
        }
        (s(&f["downloadUrl"]), safe_file_name(&s(&f["fileName"]), &[".zip"])?, cf_sha1(&f), f["fileLength"].as_u64())
    };
    if !url.starts_with("https://") {
        return Err(tr!("Rejected a download address that does not use HTTPS", "Odrzucono adres pobierania, który nie używa HTTPS"));
    }
    rep.status(&tr!("Downloading modpack “{title}”…", "Pobieram modpack „{title}”…"));
    let path = cache.join(&name);
    download(&client, &Task { url, dest: path.clone(), sha1, size }).await?;
    install_modpack_file(&path, game_dir, rep).await
}

// ------------------------------------------------------------- zainstalowane pliki

pub fn mod_base(name: &str) -> &str {
    name.strip_suffix(".disabled").filter(|b| b.ends_with(".jar")).unwrap_or(name)
}

pub fn list_mods(game_dir: &Path) -> Vec<InstalledFile> {
    let index = load_index(&mods_index(game_dir));
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(game_dir.join("mods")) {
        for e in rd.flatten() {
            let file = e.file_name().to_string_lossy().to_string();
            if !e.path().is_file() || !(file.ends_with(".jar") || file.ends_with(".jar.disabled")) {
                continue;
            }
            let base = mod_base(&file).to_string();
            let info = index.get(&base).cloned().unwrap_or(Value::Null);
            out.push(InstalledFile {
                enabled: file.ends_with(".jar"),
                title: info["title"].as_str().map(str::to_string).unwrap_or_else(|| base.clone()),
                source: info["source"].as_str().unwrap_or("manual").to_string(),
                id: s(&info["id"]),
                file,
                base,
            });
        }
    }
    out.sort_by_key(|m| m.title.to_lowercase());
    out
}

fn mod_paths(game_dir: &Path, file: &str) -> Result<(String, PathBuf, PathBuf), String> {
    let base = safe_file_name(mod_base(file), &[".jar"])?;
    let dir = game_dir.join("mods");
    Ok((base.clone(), dir.join(&base), dir.join(format!("{base}.disabled"))))
}

pub fn toggle_mod(game_dir: &Path, file: &str) -> Result<bool, String> {
    let (_, on, off) = mod_paths(game_dir, file)?;
    if on.exists() {
        std::fs::rename(&on, &off).map_err(|e| e.to_string())?;
        Ok(false)
    } else if off.exists() {
        std::fs::rename(&off, &on).map_err(|e| e.to_string())?;
        Ok(true)
    } else {
        Err(tr!("File not found: {}", "Nie znaleziono pliku {}", on.display()))
    }
}

pub fn remove_mod(game_dir: &Path, file: &str) -> Result<(), String> {
    let (base, on, off) = mod_paths(game_dir, file)?;
    for p in [on, off] {
        if p.exists() {
            std::fs::remove_file(&p).map_err(|e| e.to_string())?;
        }
    }
    let path = mods_index(game_dir);
    let mut index = load_index(&path);
    if index.remove(&base).is_some() {
        save_index(&path, &index)?;
    }
    Ok(())
}

pub fn list_packs(folder: &Path, index_path: &Path) -> Vec<InstalledFile> {
    let index = load_index(index_path);
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(folder) {
        for e in rd.flatten() {
            let file = e.file_name().to_string_lossy().to_string();
            let is_dir = e.path().is_dir();
            if !(is_dir || file.to_lowercase().ends_with(".zip")) {
                continue;
            }
            let info = index.get(&file).cloned().unwrap_or(Value::Null);
            out.push(InstalledFile {
                title: info["title"].as_str().map(str::to_string).unwrap_or_else(|| file.clone()),
                source: info["source"].as_str().unwrap_or("manual").to_string(),
                id: s(&info["id"]),
                base: file.clone(),
                enabled: true,
                file,
            });
        }
    }
    out.sort_by_key(|m| m.title.to_lowercase());
    out
}

pub fn remove_pack(folder: &Path, index_path: &Path, name: &str) -> Result<(), String> {
    if name.is_empty() || name.contains('/') || name.contains('\\') || name == "." || name == ".." {
        return Err(tr!("Invalid name: {name}", "Nieprawidłowa nazwa: {name}"));
    }
    let path = folder.join(name);
    if path.is_dir() {
        std::fs::remove_dir_all(&path).map_err(|e| e.to_string())?;
    } else if path.exists() {
        std::fs::remove_file(&path).map_err(|e| e.to_string())?;
    }
    let mut index = load_index(index_path);
    if index.remove(name).is_some() {
        save_index(index_path, &index)?;
    }
    Ok(())
}

pub fn list_worlds(game_dir: &Path) -> Vec<String> {
    let mut worlds: Vec<(std::time::SystemTime, String)> = std::fs::read_dir(game_dir.join("saves"))
        .map(|rd| {
            rd.flatten()
                .filter_map(|e| {
                    let level = e.path().join("level.dat");
                    let time = std::fs::metadata(&level).and_then(|m| m.modified()).ok()?;
                    Some((time, e.file_name().to_string_lossy().to_string()))
                })
                .collect()
        })
        .unwrap_or_default();
    worlds.sort_by(|a, b| b.0.cmp(&a.0));
    worlds.into_iter().map(|(_, n)| n).collect()
}

/// Klucze "zrodlo:id" zainstalowanych projektow (do znaczkow ✓ na liscie).
pub fn installed_keys(game_dir: &Path, kind: &str) -> Vec<String> {
    let path = match kind {
        "mod" => mods_index(game_dir),
        "resourcepack" => resourcepack_index(game_dir),
        _ => return vec![],
    };
    load_index(&path).values().map(|i| format!("{}:{}", s(&i["source"]), s(&i["id"]))).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_paths() {
        let base = Path::new("/gra");
        assert_eq!(safe_join(base, "mods/a.jar").unwrap(), PathBuf::from("/gra/mods/a.jar"));
        assert_eq!(safe_join(base, "config\\x.toml").unwrap(), PathBuf::from("/gra/config/x.toml"));
        for bad in ["../x", "/etc/passwd", "C:/x", "a/../../b", "", "a/b:c"] {
            assert!(safe_join(base, bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn file_names() {
        assert_eq!(safe_file_name("x/../sodium.jar", &[".jar"]).unwrap(), "sodium.jar");
        assert!(safe_file_name(".hidden.jar", &[".jar"]).is_err());
        assert!(safe_file_name("a.exe", &[".jar"]).is_err());
        assert_eq!(mod_base("a.jar.disabled"), "a.jar");
        assert_eq!(mod_base("a.jar"), "a.jar");
    }

    #[test]
    fn loaders() {
        assert_eq!(mr_loaders("quilt"), vec!["quilt", "fabric"]);
        assert_eq!(cf_loader("neoforge"), Some(6));
        assert!(mr_loaders("vanilla").is_empty());
        assert_eq!(query(&[("q", "a b/ł".into())]), "q=a%20b%2F%C5%82");
    }
}
