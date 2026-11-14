//! Logowanie kontem Microsoft: kod urzadzenia -> Xbox Live -> XSTS -> Minecraft Services.
//! Zapisany refresh token jest wspolny z wersja w Pythonie (plik auth.json).

use serde::Serialize;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};

use crate::{root_dir, user_agent};

/// ID aplikacji "Zorza Client" z Azure (nie jest sekretem). Wartosc w config.json ma pierwszenstwo.
pub const MSA_CLIENT_ID: &str = "cfe6949e-3ec1-4fc4-9310-fe7a808956cc";
const MSA: &str = "https://login.microsoftonline.com/consumers/oauth2/v2.0";
const SCOPE: &str = "XboxLive.signin offline_access";

#[derive(Clone)]
pub struct Account {
    pub name: String,
    pub uuid: String,
    pub token: String,
}

#[derive(Serialize, Clone)]
pub struct PublicAccount {
    pub name: String,
    pub uuid: String,
}

impl From<&Account> for PublicAccount {
    fn from(a: &Account) -> Self {
        PublicAccount { name: a.name.clone(), uuid: a.uuid.clone() }
    }
}

#[derive(Default)]
pub struct AuthState {
    pub account: Mutex<Option<Account>>,
    pub cancel: Mutex<Option<Arc<AtomicBool>>>,
}

#[derive(Serialize, Clone)]
struct CodeEvent {
    code: String,
    uri: String,
}

fn auth_file() -> std::path::PathBuf {
    root_dir().join("auth.json")
}

pub fn has_saved_login() -> bool {
    auth_file().exists()
}

pub fn forget_saved_login() {
    let _ = std::fs::remove_file(auth_file());
}

fn client_id() -> String {
    std::fs::read_to_string(root_dir().join("config.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| v["msa_client_id"].as_str().map(str::to_string))
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| MSA_CLIENT_ID.to_string())
}

fn saved_refresh_token() -> Option<String> {
    let text = std::fs::read_to_string(auth_file()).ok()?;
    let v: Value = serde_json::from_str(&text).ok()?;
    v["refresh_token"].as_str().map(str::to_string).filter(|s| !s.is_empty())
}

fn save_refresh_token(token: &str) {
    let path = auth_file();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&path, json!({ "refresh_token": token }).to_string());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
}

fn net_error(e: reqwest::Error) -> String {
    format!("Błąd połączenia: {e}")
}

/// POST, ktory zwraca (status, json) takze dla bledow HTTP.
async fn post_form(client: &reqwest::Client, url: &str, form: &[(&str, &str)]) -> Result<(u16, Value), String> {
    let resp = client.post(url).form(form).send().await.map_err(net_error)?;
    let status = resp.status().as_u16();
    Ok((status, resp.json::<Value>().await.unwrap_or(Value::Null)))
}

async fn post_json(client: &reqwest::Client, url: &str, body: &Value) -> Result<(u16, Value), String> {
    let resp = client
        .post(url)
        .header("Accept", "application/json")
        .json(body)
        .send()
        .await
        .map_err(net_error)?;
    let status = resp.status().as_u16();
    Ok((status, resp.json::<Value>().await.unwrap_or(Value::Null)))
}

fn describe(v: &Value) -> String {
    v["error_description"]
        .as_str()
        .or_else(|| v["error"].as_str())
        .unwrap_or("nieznany błąd")
        .to_string()
}

/// Czeka `secs` sekund, sprawdzajac co chwile, czy logowanie anulowano.
async fn wait(secs: u64, cancel: &AtomicBool) -> Result<(), String> {
    let end = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < end {
        if cancel.load(Ordering::Relaxed) {
            return Err("Logowanie anulowane".into());
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    Ok(())
}

async fn msa_token(app: &AppHandle, client: &reqwest::Client, cid: &str, cancel: &AtomicBool) -> Result<Value, String> {
    if let Some(refresh) = saved_refresh_token() {
        let (status, tok) = post_form(
            client,
            &format!("{MSA}/token"),
            &[("client_id", cid), ("grant_type", "refresh_token"), ("refresh_token", &refresh), ("scope", SCOPE)],
        )
        .await?;
        if status == 200 {
            return Ok(tok);
        }
    }
    let (status, dev) =
        post_form(client, &format!("{MSA}/devicecode"), &[("client_id", cid), ("scope", SCOPE)]).await?;
    if status != 200 {
        return Err(format!("Microsoft odrzucił aplikację: {}", describe(&dev)));
    }
    let device_code = dev["device_code"].as_str().ok_or("Brak kodu urządzenia w odpowiedzi")?.to_string();
    let code = dev["user_code"].as_str().unwrap_or_default().to_string();
    let uri = dev["verification_uri"].as_str().unwrap_or("https://www.microsoft.com/link").to_string();
    let _ = app.emit("login-code", CodeEvent { code, uri: uri.clone() });
    if uri.starts_with("https://") {
        let _ = tauri_plugin_opener::open_url(uri, None::<&str>);
    }
    let deadline = Instant::now() + Duration::from_secs(dev["expires_in"].as_u64().unwrap_or(900));
    let mut interval = dev["interval"].as_u64().unwrap_or(5);
    while Instant::now() < deadline {
        wait(interval, cancel).await?;
        let (status, tok) = post_form(
            client,
            &format!("{MSA}/token"),
            &[
                ("client_id", cid),
                ("device_code", &device_code),
                ("grant_type", "urn:ietf:params:oauth:grant-type:device_code"),
            ],
        )
        .await?;
        if status == 200 {
            return Ok(tok);
        }
        match tok["error"].as_str() {
            Some("authorization_pending") => {}
            Some("slow_down") => interval += 5,
            _ => return Err(format!("Logowanie przerwane: {}", describe(&tok))),
        }
    }
    Err("Kod logowania wygasł, spróbuj ponownie".into())
}

pub async fn login(app: &AppHandle, cancel: &AtomicBool) -> Result<Account, String> {
    let client = reqwest::Client::builder()
        .user_agent(user_agent())
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let cid = client_id();
    let tok = msa_token(app, &client, &cid, cancel).await?;
    if let Some(refresh) = tok["refresh_token"].as_str() {
        save_refresh_token(refresh);
    }
    let access = tok["access_token"].as_str().ok_or("Microsoft nie zwrócił tokenu")?;

    let (status, xbl) = post_json(
        &client,
        "https://user.auth.xboxlive.com/user/authenticate",
        &json!({
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": format!("d={access}")},
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"
        }),
    )
    .await?;
    if status != 200 {
        return Err(format!("Xbox Live odrzucił logowanie ({status})"));
    }
    let uhs = xbl["DisplayClaims"]["xui"][0]["uhs"].as_str().ok_or("Brak danych Xbox Live")?.to_string();
    let xbl_token = xbl["Token"].as_str().ok_or("Brak tokenu Xbox Live")?.to_string();

    let (status, xsts) = post_json(
        &client,
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        &json!({
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
            "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"
        }),
    )
    .await?;
    if status != 200 {
        let reason = match xsts["XErr"].as_u64() {
            Some(2148916233) => "to konto Microsoft nie ma profilu Xbox".to_string(),
            Some(2148916235) => "Xbox Live jest niedostępny w tym kraju".to_string(),
            Some(2148916238) => "konto dziecka: musi być dodane do rodziny Microsoft".to_string(),
            Some(code) => format!("błąd {code}"),
            None => format!("błąd {status}"),
        };
        return Err(format!("XSTS: {reason}"));
    }
    let xsts_token = xsts["Token"].as_str().ok_or("Brak tokenu XSTS")?;

    let (status, mc) = post_json(
        &client,
        "https://api.minecraftservices.com/authentication/login_with_xbox",
        &json!({ "identityToken": format!("XBL3.0 x={uhs};{xsts_token}") }),
    )
    .await?;
    if status != 200 {
        let hint = if status == 403 { " Aplikacja nie ma jeszcze zgody Mojangu na API Minecrafta." } else { "" };
        return Err(format!("Minecraft Services odrzucił logowanie ({status}).{hint}"));
    }
    let mc_token = mc["access_token"].as_str().ok_or("Brak tokenu Minecrafta")?.to_string();

    let resp = client
        .get("https://api.minecraftservices.com/minecraft/profile")
        .bearer_auth(&mc_token)
        .send()
        .await
        .map_err(net_error)?;
    if resp.status().as_u16() == 404 {
        return Err("To konto nie ma kupionego Minecraft: Java Edition".into());
    }
    let profile: Value = resp.error_for_status().map_err(|e| e.to_string())?.json().await.map_err(net_error)?;
    Ok(Account {
        name: profile["name"].as_str().unwrap_or("?").to_string(),
        uuid: profile["id"].as_str().unwrap_or_default().to_string(),
        token: mc_token,
    })
}
