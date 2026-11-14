//! Pobieranie plikow: sprawdzanie SHA-1, ponowne proby, rownolegle zadania.

use futures::stream::{self, StreamExt};
use serde_json::Value;
use sha1::{Digest, Sha1};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Task {
    pub url: String,
    pub dest: PathBuf,
    pub sha1: Option<String>,
    pub size: Option<u64>,
}

pub fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .user_agent(crate::user_agent())
        .connect_timeout(Duration::from_secs(20))
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())
}

pub async fn get_json(client: &reqwest::Client, url: &str) -> Result<Value, String> {
    client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("Błąd połączenia ({url}): {e}"))?
        .error_for_status()
        .map_err(|e| e.to_string())?
        .json::<Value>()
        .await
        .map_err(|e| format!("Nieprawidłowa odpowiedź z {url}: {e}"))
}

pub fn sha1_hex(data: &[u8]) -> String {
    let mut hasher = Sha1::new();
    hasher.update(data);
    hasher.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

fn is_done(t: &Task) -> bool {
    match std::fs::metadata(&t.dest) {
        Ok(m) => m.is_file() && t.size.map_or(true, |s| m.len() == s),
        Err(_) => false,
    }
}

fn part_path(p: &Path) -> PathBuf {
    let mut s = p.as_os_str().to_owned();
    s.push(".part");
    PathBuf::from(s)
}

async fn fetch(client: &reqwest::Client, t: &Task) -> Result<Vec<u8>, String> {
    let bytes = client
        .get(&t.url)
        .send()
        .await
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?
        .bytes()
        .await
        .map_err(|e| e.to_string())?;
    if let Some(want) = &t.sha1 {
        if !want.is_empty() && sha1_hex(&bytes) != want.to_lowercase() {
            return Err("zła suma kontrolna".into());
        }
    }
    Ok(bytes.to_vec())
}

pub async fn download(client: &reqwest::Client, t: &Task) -> Result<(), String> {
    if is_done(t) {
        return Ok(());
    }
    if let Some(dir) = t.dest.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("Nie można utworzyć {}: {e}", dir.display()))?;
    }
    let mut last = String::new();
    for attempt in 0..3u64 {
        match fetch(client, t).await {
            Ok(bytes) => {
                let tmp = part_path(&t.dest);
                std::fs::write(&tmp, &bytes).map_err(|e| format!("Nie można zapisać {}: {e}", tmp.display()))?;
                std::fs::rename(&tmp, &t.dest).map_err(|e| format!("Nie można zapisać {}: {e}", t.dest.display()))?;
                return Ok(());
            }
            Err(e) => {
                last = e;
                tokio::time::sleep(Duration::from_secs(1 + attempt)).await;
            }
        }
    }
    Err(format!("Nie udało się pobrać {}: {last}", t.url))
}

/// Pobiera brakujace pliki rownolegle. progress(gotowe, wszystkie).
pub async fn download_all<F: Fn(usize, usize)>(
    client: &reqwest::Client,
    tasks: Vec<Task>,
    workers: usize,
    progress: F,
) -> Result<(), String> {
    let mut seen = HashSet::new();
    let todo: Vec<Task> = tasks.into_iter().filter(|t| !is_done(t) && seen.insert(t.dest.clone())).collect();
    let total = todo.len();
    if total == 0 {
        return Ok(());
    }
    let mut results = stream::iter(todo.into_iter().map(|t| {
        let c = client.clone();
        async move { download(&c, &t).await }
    }))
    .buffer_unordered(workers);
    let mut done = 0;
    while let Some(r) = results.next().await {
        r?;
        done += 1;
        progress(done, total);
    }
    Ok(())
}
