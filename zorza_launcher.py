#!/usr/bin/env python3
"""Zorza Client - launcher Minecraft: Java Edition z Fabric, Quilt, Forge i NeoForge.

Profile, ustawienia i instalowanie modow z Modrinth oraz CurseForge.

Uruchamianie:   python zorza_launcher.py          (okno)
                python zorza_launcher.py --cli    (terminal)

Wymaga tylko Pythona 3.9+ (biblioteka standardowa). Obsluguje wersje gry
1.19 i nowsze (w tym 26.x). Pliki gry sa pobierane z serwerow Mojangu,
Loadery z ich oficjalnych serwerow. Logowanie wylacznie kontem Microsoft.
"""
import base64
import hashlib
import html
import json
import math
import os
import platform
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

NAME = "Zorza Client"
VERSION = "0.5.2"
UA = f"RastamanPoland/zorza-client/{VERSION} (github.com/RastamanPoland/zorza-client)"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net"
FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
NEOFORGE_LATEST = "https://maven.neoforged.net/api/maven/latest/version/releases/net/neoforged/neoforge"

LOADERS = ("vanilla", "fabric", "quilt", "forge", "neoforge")
LOADER_LABELS = {"vanilla": "Vanilla (bez modów)", "fabric": "Fabric", "quilt": "Quilt", "forge": "Forge",
                 "neoforge": "NeoForge"}
# jakie mody pasuja do loadera: Quilt uruchamia tez mody Fabric
MODRINTH_LOADERS = {"fabric": ["fabric"], "quilt": ["quilt", "fabric"], "forge": ["forge"], "neoforge": ["neoforge"]}
CURSEFORGE_LOADER = {"fabric": 4, "quilt": 4, "forge": 1, "neoforge": 6}
JAVA_RUNTIMES_URL = ("https://piston-meta.mojang.com/v1/products/java-runtime/"
                     "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
MSA = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
# ID aplikacji "Zorza Client" z Azure (nie jest sekretem). Wartosc w config.json ma pierwszenstwo.
MSA_CLIENT_ID = "cfe6949e-3ec1-4fc4-9310-fe7a808956cc"

# ---------------------------------------------------------------- platforma

OS_NAME = {"Windows": "windows", "Darwin": "osx", "Linux": "linux"}.get(platform.system(), "linux")
_machine = platform.machine().lower()
ARCH = "x64" if _machine in ("amd64", "x86_64") else "arm64" if _machine in ("arm64", "aarch64") else "x86"
CP_SEP = ";" if OS_NAME == "windows" else ":"


def root_dir() -> Path:
    if OS_NAME == "windows":
        return Path(os.environ.get("APPDATA", Path.home())) / ".zorza"
    if OS_NAME == "osx":
        return Path.home() / "Library" / "Application Support" / "zorza"
    return Path.home() / ".zorza"


ROOT = root_dir()
LIBS, ASSETS, VERSIONS = ROOT / "libraries", ROOT / "assets", ROOT / "versions"
RUNTIMES, GAME_DIR = ROOT / "runtimes", ROOT / "game"
CONFIG_FILE, AUTH_FILE = ROOT / "config.json", ROOT / "auth.json"
PROFILES_FILE, PROFILES_DIR = ROOT / "profiles.json", ROOT / "profiles"
MODRINTH = "https://api.modrinth.com/v2"
CURSEFORGE = "https://api.curseforge.com/v1"

try:  # klucz CurseForge trzymany w osobnym pliku, ktory nie trafia do repozytorium (.gitignore)
    from zorza_secrets import CURSEFORGE_API_KEY as BUILTIN_CURSEFORGE_KEY
except ImportError:
    BUILTIN_CURSEFORGE_KEY = ""


_cdn_key = {"value": ""}  # klucz CurseForge do pobierania plikow z ich CDN (wymagany od lipca 2026)


def cdn_headers(url: str) -> dict:
    host = urllib.parse.urlparse(url).hostname or ""
    if _cdn_key["value"] and (host == "forgecdn.net" or host.endswith(".forgecdn.net")):
        return {"x-api-key": _cdn_key["value"]}
    return {}


def curseforge_key(cfg: dict) -> str:
    """Wlasny klucz z ustawien ma pierwszenstwo przed wbudowanym."""
    return (cfg.get("curseforge_api_key") or BUILTIN_CURSEFORGE_KEY or "").strip()

DEFAULT_CONFIG = {
    "msa_client_id": "",      # ID aplikacji z Azure (wymaga zgody Mojangu na API)
    "game_version": "latest",  # "latest" albo np. "26.3"
    "use_fabric": True,       # stare ustawienie; profile maja wlasne pole "loader"
    "memory_mb": 4096,
    "java_path": "",          # puste = pobierz Jave od Mojangu
    "curseforge_api_key": "",  # opcjonalny wlasny klucz; puste = klucz wbudowany
}


class LauncherError(Exception):
    pass


def load_config() -> dict:
    ROOT.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
    if not cfg.get("msa_client_id"):
        cfg["msa_client_id"] = MSA_CLIENT_ID
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")
    return cfg


# --------------------------------------------------------------------- siec

def http(url, data=None, headers=None, form=False, timeout=30) -> bytes:
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, **kw):
    return json.loads(http(url, **kw))


def post_json_lenient(url, data, form=False, headers=None):
    """POST, ktory zwraca (status, json) takze dla bledow HTTP."""
    try:
        return 200, get_json(url, data=data, form=form, headers=headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode("utf-8", "replace")}


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, sha1: str = None, size: int = None) -> None:
    """Pobiera plik, jesli go nie ma (albo ma zly rozmiar). Weryfikuje SHA-1."""
    if dest.exists() and (size is None or dest.stat().st_size == size):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=dict(cdn_headers(url), **{"User-Agent": UA}))
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if sha1 and sha1_of(tmp) != sha1:
                raise LauncherError(f"Zla suma kontrolna: {url}")
            os.replace(tmp, dest)
            return
        except (OSError, LauncherError) as e:
            last = e
            time.sleep(1 + attempt)
    raise LauncherError(f"Nie udalo sie pobrac {url}: {last}")


def download_all(tasks, progress=None, workers=12) -> None:
    """tasks: lista (url, dest, sha1, size). progress(done, total)."""
    tasks = [t for t in tasks if not (t[1].exists() and (t[3] is None or t[1].stat().st_size == t[3]))]
    total = len(tasks)
    if not total:
        return
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(download, *t) for t in tasks]
        for fut in as_completed(futures):
            fut.result()
            done += 1
            if progress:
                progress(done, total)


# ------------------------------------------------------------ wersje i reguly

def rules_allow(rules, features=None) -> bool:
    """Reguly Mojangu: ostatnia pasujaca decyduje, brak regul = dozwolone."""
    if not rules:
        return True
    features = features or {}
    allowed = False
    for rule in rules:
        ok = True
        os_rule = rule.get("os", {})
        if "name" in os_rule and os_rule["name"] != OS_NAME:
            ok = False
        if "arch" in os_rule and os_rule["arch"] != ARCH:
            ok = False
        if "version" in os_rule and not re.search(os_rule["version"], platform.version()):
            ok = False
        for key, want in rule.get("features", {}).items():
            if bool(features.get(key, False)) != want:
                ok = False
        if ok:
            allowed = rule.get("action") == "allow"
    return allowed


def maven_path(name: str) -> str:
    """'grupa:artefakt:wersja[:klasyfikator][@ext]' -> sciezka w repozytorium."""
    ext = "jar"
    if "@" in name:
        name, ext = name.split("@", 1)
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = f"-{parts[3]}" if len(parts) > 3 else ""
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{classifier}.{ext}"


def lib_key(name: str) -> str:
    parts = name.split("@")[0].split(":")
    return ":".join(parts[:2] + parts[3:4])  # bez wersji: duplikaty nadpisujemy


def cfg_loader(cfg: dict) -> str:
    loader = cfg.get("loader")
    if loader in LOADERS:
        return loader
    return "fabric" if cfg.get("use_fabric") else "vanilla"


def merge_inherited(parent: dict, child: dict) -> dict:
    """Laczy wersje z "inheritsFrom" (Forge, NeoForge) z wersja vanilla, jak oficjalny launcher."""
    v = dict(parent)
    for key in ("mainClass", "javaVersion", "assetIndex", "assets"):
        if child.get(key):
            v[key] = child[key]
    v["libraries"] = list(parent.get("libraries", [])) + list(child.get("libraries", []))
    v["arguments"] = {k: list(parent.get("arguments", {}).get(k, [])) + list(child.get("arguments", {}).get(k, []))
                      for k in ("jvm", "game")}
    return v


def neoforge_prefix(mc: str) -> str:
    """1.21.1 -> '21.1.', 1.21 -> '21.0.', 26.1 -> '26.1.0.', 26.1.2 -> '26.1.2.'"""
    parts = mc.split(".")
    if parts[0] == "1":
        return f"{parts[1]}.{parts[2] if len(parts) > 2 else 0}."
    return f"{parts[0]}.{parts[1] if len(parts) > 1 else 0}.{parts[2] if len(parts) > 2 else 0}."


def forge_like_version(loader: str, mc: str, pin: str = "") -> str:
    if pin:
        return pin
    if loader == "forge":
        promos = get_json(FORGE_PROMOS).get("promos", {})
        version = promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
        if not version:
            raise LauncherError(f"Forge nie ma jeszcze wersji dla Minecraft {mc}")
        return version
    prefix = neoforge_prefix(mc)
    version = str(get_json(f"{NEOFORGE_LATEST}?{urllib.parse.urlencode({'filter': prefix})}").get("version") or "")
    if not version.startswith(prefix):
        raise LauncherError(f"NeoForge nie ma jeszcze wersji dla Minecraft {mc}")
    return version


def install_forge_like(loader: str, mc: str, pin: str, vanilla: dict, cfg: dict, log=print, progress=None):
    """Uruchamia oficjalny instalator Forge/NeoForge w trybie bez okna. Zwraca (wersja, json wersji)."""
    label = LOADER_LABELS[loader]
    version = forge_like_version(loader, mc, pin)
    if loader == "forge":
        url = f"{FORGE_MAVEN}/{mc}-{version}/forge-{mc}-{version}-installer.jar"
    else:
        url = f"{NEOFORGE_MAVEN}/{version}/neoforge-{version}-installer.jar"
    installer = ROOT / "cache" / "installers" / url.rsplit("/", 1)[1]
    log(f"Pobieram instalator {label} {version}…")
    download(url, installer)
    try:
        with zipfile.ZipFile(installer) as zf:
            vid = json.loads(zf.read("install_profile.json").decode("utf-8")).get("version")
    except (zipfile.BadZipFile, KeyError, ValueError):
        installer.unlink(missing_ok=True)
        raise LauncherError(f"Instalator {label} {version} jest uszkodzony, spróbuj ponownie")
    if not vid or "/" in vid or "\\" in vid or vid.startswith("."):
        raise LauncherError(f"Nieoczekiwana nazwa wersji w instalatorze {label}: {vid}")
    vjson = VERSIONS / vid / f"{vid}.json"
    if not vjson.exists():
        profiles = ROOT / "launcher_profiles.json"  # instalator nie dziala bez tego pliku
        if not profiles.exists():
            profiles.write_text(json.dumps({"profiles": {}}), "utf-8")
        java = ensure_java(vanilla, cfg, log, progress)
        log(f"Instaluję {label} {version}. Za pierwszym razem może to potrwać kilka minut…")
        flags = 0x08000000 if OS_NAME == "windows" else 0  # bez okna konsoli
        try:
            proc = subprocess.run([java, "-jar", str(installer), "--installClient", str(ROOT)],
                                  cwd=installer.parent, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=1800, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise LauncherError(f"Instalator {label} nie skończył pracy w 30 minut")
        if proc.returncode != 0 or not vjson.exists():
            tail = (proc.stdout or "")[-1200:] + (proc.stderr or "")[-600:]
            raise LauncherError(f"Instalator {label} zakończył się błędem (kod {proc.returncode}):\n{tail.strip()}")
    return version, json.loads(vjson.read_text("utf-8"))


def resolve_version(cfg, log=print, progress=None) -> dict:
    """Zwraca scalony opis wersji: vanilla plus wybrany loader."""
    manifest = get_json(MANIFEST_URL)
    vid = cfg["game_version"]
    if vid == "latest":
        vid = manifest["latest"]["release"]
    entry = next((v for v in manifest["versions"] if v["id"] == vid), None)
    if not entry:
        raise LauncherError(f"Nie ma wersji {vid} w manifescie Mojangu")
    vfile = VERSIONS / vid / f"{vid}.json"
    download(entry["url"], vfile, entry.get("sha1"))
    v = json.loads(vfile.read_text("utf-8"))
    if "arguments" not in v:
        raise LauncherError("Ta wersja gry jest za stara (obslugiwane 1.19+)")
    v["_id"], v["_mc"], v["_fabric"], v["_loader"] = vid, vid, None, None
    loader = cfg_loader(cfg)
    pin = cfg.get("loader_version") or cfg.get("fabric_loader") or ""
    if loader in ("fabric", "quilt"):
        meta, label = (FABRIC_META, "Fabric Loader") if loader == "fabric" else (QUILT_META, "Quilt Loader")
        loaders = get_json(f"{meta}/versions/loader/{urllib.parse.quote(vid)}")
        if not loaders:
            raise LauncherError(f"{LOADER_LABELS[loader]} nie wspiera jeszcze wersji {vid}. "
                                "Wybierz starszą wersję gry albo inny loader.")
        pick = next((l for l in loaders if pin and l["loader"]["version"] == pin), None)
        if pin and not pick:
            log(f"{label} {pin} nie pasuje do wersji {vid}, używam najnowszego stabilnego")
        if loader == "fabric":
            pick = pick or next((l for l in loaders if l["loader"].get("stable")), loaders[0])
        else:
            pick = pick or next((l for l in loaders if not re.search(r"beta|pre|rc", l["loader"]["version"])), loaders[0])
        lv = pick["loader"]["version"]
        log(f"{label} {lv}")
        prof = get_json(f"{meta}/versions/loader/{urllib.parse.quote(vid)}/{urllib.parse.quote(lv)}/profile/json")
        v = merge_inherited(v, prof)
        if loader == "fabric":
            v["_fabric"] = lv
        v["_loader"] = (loader, lv)
    elif loader in ("forge", "neoforge"):
        version, child = install_forge_like(loader, vid, pin, v, cfg, log, progress)
        v = merge_inherited(v, child)
        v["_id"], v["_loader"] = child.get("id") or vid, (loader, version)
    return v


def collect_libraries(v):
    """Zwraca (zadania_pobierania, classpath). Pozniejsze wpisy (Fabric) wygrywaja."""
    chosen = {}
    for lib in v["libraries"]:
        if not rules_allow(lib.get("rules")) or "name" not in lib:
            continue
        art = lib.get("downloads", {}).get("artifact")
        if art:
            path = LIBS / (art.get("path") or maven_path(lib["name"]))
            task = (art["url"], path, art.get("sha1"), art.get("size")) if art.get("url") else None
        elif lib.get("url"):  # styl Fabrica: nazwa + adres repozytorium
            path = LIBS / maven_path(lib["name"])
            task = (lib["url"].rstrip("/") + "/" + maven_path(lib["name"]), path, lib.get("sha1"), lib.get("size"))
        elif "downloads" in lib:
            continue  # tylko natywne pliki starego typu
        else:
            path, task = LIBS / maven_path(lib["name"]), None  # plik utworzony przez instalator
        chosen[lib_key(lib["name"])] = (task, path)
    return [t for t, _ in chosen.values() if t], [p for _, p in chosen.values()]


def collect_assets(v):
    idx = v["assetIndex"]
    idx_file = ASSETS / "indexes" / f"{idx['id']}.json"
    download(idx["url"], idx_file, idx.get("sha1"))
    objects = json.loads(idx_file.read_text("utf-8"))["objects"]
    tasks = {}
    for obj in objects.values():
        h = obj["hash"]
        tasks[h] = (f"{RESOURCES_URL}/{h[:2]}/{h}", ASSETS / "objects" / h[:2] / h, h, obj.get("size"))
    return list(tasks.values())


# --------------------------------------------------------------------- Java

def java_platform_key():
    return {
        ("windows", "x64"): "windows-x64", ("windows", "x86"): "windows-x86",
        ("windows", "arm64"): "windows-arm64",
        ("osx", "x64"): "mac-os", ("osx", "arm64"): "mac-os-arm64",
        ("linux", "x64"): "linux", ("linux", "x86"): "linux-i386",
    }.get((OS_NAME, ARCH))


def ensure_java(v, cfg, log=print, progress=None) -> str:
    if cfg.get("java_path"):
        return cfg["java_path"]
    jv = v.get("javaVersion", {})
    component, major = jv.get("component", "java-runtime-gamma"), jv.get("majorVersion", "?")
    exe = "java.exe" if OS_NAME == "windows" else "java"
    key = java_platform_key()
    entries = get_json(JAVA_RUNTIMES_URL).get(key, {}).get(component, []) if key else []
    if not entries:
        system = shutil.which("java")
        if not system:
            raise LauncherError(f"Brak Javy {major} dla tej platformy. Zainstaluj ja i ustaw 'java_path'.")
        log(f"UWAGA: uzywam systemowej Javy ({system}); gra wymaga wersji {major}")
        return system
    home = RUNTIMES / component / key
    files = get_json(entries[0]["manifest"]["url"])["files"]
    tasks, executables = [], []
    for rel, info in files.items():
        target = home / rel
        if info["type"] == "directory":
            target.mkdir(parents=True, exist_ok=True)
        elif info["type"] == "file":
            raw = info["downloads"]["raw"]
            tasks.append((raw["url"], target, raw.get("sha1"), raw.get("size")))
            if info.get("executable"):
                executables.append(target)
    log(f"Java {major} ({component})")
    download_all(tasks, progress)
    if OS_NAME != "windows":
        for rel, info in files.items():
            if info["type"] == "link":
                link = home / rel
                if not link.exists() and not link.is_symlink():
                    link.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(info["target"], link)
        for f in executables:
            f.chmod(0o755)
    for candidate in (home / "bin" / exe, home / "jre.bundle" / "Contents" / "Home" / "bin" / exe):
        if candidate.exists():
            return str(candidate)
    raise LauncherError("Pobrano Jave, ale nie znaleziono pliku wykonywalnego")


# ---------------------------------------------------- logowanie Microsoft

class MicrosoftAuth:
    """Device code flow -> Xbox Live -> XSTS -> Minecraft Services."""

    def __init__(self, client_id: str):
        if not client_id:
            raise LauncherError(
                f"Brak 'msa_client_id' w {CONFIG_FILE}.\n"
                "Zarejestruj aplikacje w Azure (konta osobiste, 'public client flows' wlaczone), "
                "wpisz jej ID do configu i zglos ja Mojangowi do zatwierdzenia.")
        self.client_id = client_id
        self.cancelled = threading.Event()

    def _msa_token(self, show_code):
        saved = json.loads(AUTH_FILE.read_text("utf-8")) if AUTH_FILE.exists() else {}
        if saved.get("refresh_token"):
            code, tok = post_json_lenient(f"{MSA}/token", form=True, data={
                "client_id": self.client_id, "grant_type": "refresh_token",
                "refresh_token": saved["refresh_token"], "scope": "XboxLive.signin offline_access"})
            if code == 200:
                return tok
        code, dev = post_json_lenient(f"{MSA}/devicecode", form=True, data={
            "client_id": self.client_id, "scope": "XboxLive.signin offline_access"})
        if code != 200:
            raise LauncherError(f"Microsoft odrzucił aplikację: {dev.get('error_description', dev)}")
        show_code(dev["user_code"], dev["verification_uri"])
        deadline = time.time() + int(dev.get("expires_in", 900))
        interval = int(dev.get("interval", 5))
        while time.time() < deadline:
            if self.cancelled.wait(interval):
                raise LauncherError("Logowanie anulowane")
            code, tok = post_json_lenient(f"{MSA}/token", form=True, data={
                "client_id": self.client_id, "device_code": dev["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            if code == 200:
                return tok
            err = tok.get("error")
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                raise LauncherError(f"Logowanie przerwane: {tok.get('error_description', err)}")
        raise LauncherError("Kod logowania wygasł, spróbuj ponownie")

    def login(self, show_code) -> dict:
        tok = self._msa_token(show_code)
        if tok.get("refresh_token"):
            AUTH_FILE.write_text(json.dumps({"refresh_token": tok["refresh_token"]}), "utf-8")
            if OS_NAME != "windows":
                AUTH_FILE.chmod(0o600)
        code, xbl = post_json_lenient("https://user.auth.xboxlive.com/user/authenticate", {
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                           "RpsTicket": "d=" + tok["access_token"]},
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
        if code != 200:
            raise LauncherError(f"Xbox Live odrzucil logowanie ({code})")
        uhs = xbl["DisplayClaims"]["xui"][0]["uhs"]
        code, xsts = post_json_lenient("https://xsts.auth.xboxlive.com/xsts/authorize", {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
            "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
        if code != 200:
            reasons = {2148916233: "to konto Microsoft nie ma profilu Xbox",
                       2148916235: "Xbox Live jest niedostepny w tym kraju",
                       2148916238: "konto dziecka: musi byc dodane do rodziny Microsoft"}
            raise LauncherError("XSTS: " + reasons.get(xsts.get("XErr"), f"blad {xsts.get('XErr', code)}"))
        code, mc = post_json_lenient("https://api.minecraftservices.com/authentication/login_with_xbox",
                                     {"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
        if code != 200:
            hint = (" Aplikacja nie ma jeszcze zgody Mojangu na API Minecrafta."
                    if code == 403 else "")
            raise LauncherError(f"Minecraft Services odrzucił logowanie ({code}).{hint}")
        try:
            prof = get_json("https://api.minecraftservices.com/minecraft/profile",
                            headers={"Authorization": "Bearer " + mc["access_token"]})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise LauncherError("To konto nie ma kupionego Minecraft: Java Edition")
            raise
        return {"name": prof["name"], "uuid": prof["id"], "token": mc["access_token"]}


# -------------------------------------------------------------- uruchamianie

def build_command(v, account, java, classpath, cfg, game_dir=None) -> list:
    vid = v["_id"]
    client_jar = VERSIONS / vid / f"{vid}.jar"
    natives = VERSIONS / vid / "natives"
    natives.mkdir(parents=True, exist_ok=True)
    values = {
        "auth_player_name": account["name"], "auth_uuid": account["uuid"],
        "auth_access_token": account["token"], "auth_xuid": "", "clientid": "",
        "user_type": "msa", "user_properties": "{}",
        "version_name": vid, "version_type": v.get("type", "release"),
        "game_directory": str(game_dir or GAME_DIR), "assets_root": str(ASSETS),
        "assets_index_name": v["assetIndex"]["id"],
        "natives_directory": str(natives), "library_directory": str(LIBS),
        "launcher_name": NAME, "launcher_version": VERSION,
        "classpath_separator": CP_SEP,
        "classpath": CP_SEP.join(str(p) for p in classpath + [client_jar]),
    }

    def expand(args):
        out = []
        for arg in args:
            if isinstance(arg, dict):
                if not rules_allow(arg.get("rules"), features={}):
                    continue
                val = arg["value"]
                arg_list = val if isinstance(val, list) else [val]
            else:
                arg_list = [arg]
            for a in arg_list:
                out.append(re.sub(r"\$\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), a))
        return out

    cmd = [java, f"-Xmx{int(cfg['memory_mb'])}M"]
    cmd += [a for a in str(cfg.get("jvm_args", "")).split() if a]
    cmd += expand(v["arguments"]["jvm"])
    cmd += [v["mainClass"]]
    cmd += expand(v["arguments"]["game"])
    return cmd


def prepare_and_launch(cfg, account, log=print, progress=None, game_dir=None) -> subprocess.Popen:
    game_dir = game_dir or GAME_DIR
    for d in (LIBS, ASSETS, VERSIONS, RUNTIMES, game_dir / "mods"):
        d.mkdir(parents=True, exist_ok=True)
    log("Sprawdzam wersję…")
    v = resolve_version(cfg, log, progress)
    vid, mc = v["_id"], v.get("_mc", v["_id"])
    log(f"Minecraft {mc}" + (f" + {LOADER_LABELS[v['_loader'][0]]} {v['_loader'][1]}" if v.get("_loader") else ""))
    lib_tasks, classpath = collect_libraries(v)
    client = v["downloads"]["client"]
    client_task = (client["url"], VERSIONS / mc / f"{mc}.jar", client.get("sha1"), client.get("size"))
    log("Pobieram biblioteki i grę…")
    download_all(lib_tasks + [client_task], progress)
    missing = [p for p in classpath if not p.exists()]
    if missing:
        raise LauncherError(f"Brakuje biblioteki {missing[0].name}. Usuń folder versions/{vid} i spróbuj ponownie.")
    if vid != mc and not (VERSIONS / vid / f"{vid}.jar").exists():
        shutil.copyfile(VERSIONS / mc / f"{mc}.jar", VERSIONS / vid / f"{vid}.jar")  # jak oficjalny launcher
    log("Pobieram zasoby…")
    download_all(collect_assets(v), progress, workers=16)
    java = ensure_java(v, cfg, log, progress)
    cmd = build_command(v, account, java, classpath, cfg, game_dir)
    log(f"Uruchamiam grę jako {account['name']}…")
    return subprocess.Popen(cmd, cwd=game_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")


# ------------------------------------------------------------ profile i mody

_latest_release = None


def fetch_manifest():
    return get_json(MANIFEST_URL)


def resolve_game_version(version: str) -> str:
    """Zamienia 'latest' na numer najnowszego wydania (wynik jest zapamietywany)."""
    global _latest_release
    if version != "latest":
        return version
    if _latest_release is None:
        _latest_release = fetch_manifest()["latest"]["release"]
    return _latest_release


def list_release_versions() -> list:
    """Wydania obslugiwane przez launcher (1.19+), od najnowszego."""
    out = []
    for v in fetch_manifest()["versions"]:
        if v.get("type") != "release":
            continue
        parts = v["id"].split(".")
        try:
            major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            continue
        if major > 1 or (major == 1 and minor >= 19):
            out.append(v["id"])
    return out


def save_config(cfg: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")


def save_profiles(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def load_profiles(cfg: dict = None) -> dict:
    """Profil = osobny folder gry (mody, swiaty, ustawienia) + wersja + loader."""
    data = None
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text("utf-8"))
        except ValueError:
            data = None
    if not data or not data.get("profiles"):
        cfg = cfg or {}
        data = {"selected": "default", "profiles": {"default": {
            "name": "Domyślny",
            "game_version": cfg.get("game_version", "latest"),
            "loader": cfg_loader(dict(cfg, use_fabric=cfg.get("use_fabric", True))),
        }}}
        save_profiles(data)
    changed = False
    for p in data["profiles"].values():  # profile z wersji 0.4 i starszych
        if p.get("loader") not in LOADERS:
            p["loader"] = "fabric" if p.get("use_fabric", True) else "vanilla"
            changed = True
        if "use_fabric" in p:
            p.pop("use_fabric")
            changed = True
        if p.get("fabric_loader"):
            p["loader_version"] = p.pop("fabric_loader")
            changed = True
    if changed:
        save_profiles(data)
    if data.get("selected") not in data["profiles"]:
        data["selected"] = next(iter(data["profiles"]))
    return data


def profile_dir(profile_id: str) -> Path:
    return GAME_DIR if profile_id == "default" else PROFILES_DIR / profile_id


def new_profile_id(name: str, existing) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "profil"
    pid, n = base, 2
    while pid in existing or pid == "default" or (PROFILES_DIR / pid).exists():
        pid, n = f"{base}-{n}", n + 1
    return pid


def valid_game_version(text: str) -> bool:
    return text == "latest" or re.fullmatch(r"\d+(\.\d+){1,2}", text) is not None


def safe_file_name(name: str, exts) -> str:
    name = Path(str(name).replace("\\", "/")).name
    if name.startswith(".") or not name.lower().endswith(tuple(exts)):
        raise LauncherError(f"Nieoczekiwana nazwa pliku: {name}")
    return name


def safe_jar_name(name: str) -> str:
    return safe_file_name(name, (".jar",))


def _load_index(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(path: Path, index: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), "utf-8")


def resourcepack_index(game_dir: Path) -> Path:
    return game_dir / "zorza_resourcepacks.json"


def mods_index_file(game_dir: Path) -> Path:
    return game_dir / "zorza_mods.json"


def load_mods_index(game_dir: Path) -> dict:
    return _load_index(mods_index_file(game_dir))


def save_mods_index(game_dir: Path, index: dict) -> None:
    _save_index(mods_index_file(game_dir), index)


def mod_base(name: str) -> str:
    """'sodium.jar.disabled' -> 'sodium.jar' (wylaczone mody maja dopisek .disabled, jak w Prism)."""
    return name[: -len(".disabled")] if name.endswith(".jar.disabled") else name


def list_installed(game_dir: Path) -> list:
    """Pliki .jar i .jar.disabled z folderu mods, z tytulami modow zainstalowanych przez launcher."""
    index = load_mods_index(game_dir)
    mods_dir = game_dir / "mods"
    out = []
    if mods_dir.is_dir():
        for path in sorted(mods_dir.iterdir(), key=lambda x: x.name.lower()):
            if not path.is_file() or not (path.name.endswith(".jar") or path.name.endswith(".jar.disabled")):
                continue
            base = mod_base(path.name)
            info = index.get(base, {})
            out.append({"file": path.name, "base": base, "enabled": path.name.endswith(".jar"),
                        "title": info.get("title", base), "source": info.get("source", "ręcznie"),
                        "id": info.get("id")})
    return out


def _mod_paths(game_dir: Path, filename: str):
    base = safe_jar_name(mod_base(Path(str(filename).replace("\\", "/")).name))
    mods_dir = game_dir / "mods"
    return base, mods_dir / base, mods_dir / (base + ".disabled")


def toggle_mod(game_dir: Path, filename: str) -> bool:
    """Wlacza albo wylacza mod. Zwraca True, jesli mod jest teraz wlaczony."""
    _, on, off = _mod_paths(game_dir, filename)
    if on.exists():
        os.replace(on, off)
        return False
    if off.exists():
        os.replace(off, on)
        return True
    raise LauncherError(f"Nie znaleziono pliku {on.name}")


def remove_mod(game_dir: Path, filename: str) -> None:
    base, on, off = _mod_paths(game_dir, filename)
    for path in (on, off):
        if path.exists():
            path.unlink()
    index = load_mods_index(game_dir)
    if index.pop(base, None) is not None:
        save_mods_index(game_dir, index)


def copy_profile_files(src: Path, dst: Path) -> None:
    if dst.exists():
        raise LauncherError(f"Folder {dst} już istnieje")
    if src.exists():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("logs", "crash-reports"))
    else:
        (dst / "mods").mkdir(parents=True)


def _store_file(target_dir, index_path, url, filename, exts, sha1, size, source, project_id, title, version):
    """Pobiera plik, podmienia starsza wersje tego samego projektu i zapisuje wpis w indeksie."""
    if not str(url).startswith("https://"):
        raise LauncherError("Odrzucono adres pobierania, który nie używa HTTPS")
    name = safe_file_name(filename, exts)
    index = _load_index(index_path)
    for old, info in list(index.items()):
        if info.get("source") == source and str(info.get("id")) == str(project_id) and old != name:
            for old_path in (target_dir / old, target_dir / (old + ".disabled")):
                if old_path.exists():
                    old_path.unlink()
            del index[old]
    disabled = target_dir / (name + ".disabled")
    if disabled.exists():
        disabled.unlink()  # nowa wersja jest wlaczona, bez duplikatu
    download(url, target_dir / name, sha1, size)
    index[name] = {"source": source, "id": str(project_id), "title": title, "version": version}
    _save_index(index_path, index)


def _store_mod(game_dir, url, filename, sha1, size, source, project_id, title, version):
    _store_file(game_dir / "mods", mods_index_file(game_dir), url, filename, (".jar",), sha1, size,
                source, project_id, title, version)


def modrinth_search(query: str, game_version: str, limit: int = 30, offset: int = 0, sort: str = None,
                    loader: str = "fabric") -> list:
    """Bez zapytania zwraca najpopularniejsze mody (sortowanie po liczbie pobran)."""
    facets = [["project_type:mod"], [f"categories:{x}" for x in MODRINTH_LOADERS[loader]]]
    facets += [[f"versions:{game_version}"]] if game_version else []
    params = {"limit": limit, "offset": offset, "facets": json.dumps(facets),
              "index": sort or ("relevance" if query else "downloads")}
    if query:
        params["query"] = query
    hits = get_json(f"{MODRINTH}/search?{urllib.parse.urlencode(params)}").get("hits", [])
    return [_modrinth_hit(h, "mod") for h in hits]


def modrinth_install(project_id, game_version, game_dir, log=print, title=None, _seen=None, loader="fabric") -> None:
    seen = _seen if _seen is not None else set()
    if project_id in seen:
        return
    seen.add(project_id)
    pid = urllib.parse.quote(str(project_id), safe="")
    query = urllib.parse.urlencode({"loaders": json.dumps(MODRINTH_LOADERS[loader]),
                                    "game_versions": json.dumps([game_version])})
    versions = get_json(f"{MODRINTH}/project/{pid}/version?{query}")
    if title is None:
        title = get_json(f"{MODRINTH}/project/{pid}").get("title", str(project_id))
    if not versions:
        raise LauncherError(f"„{title}” nie ma wersji dla Minecraft {game_version} z {LOADER_LABELS[loader]}")
    ver = versions[0]
    files = ver.get("files") or []
    if not files:
        raise LauncherError(f"„{title}” nie ma pliku do pobrania")
    f = next((x for x in files if x.get("primary")), files[0])
    _store_mod(game_dir, f["url"], f["filename"], f.get("hashes", {}).get("sha1"), f.get("size"),
               "modrinth", project_id, title, ver.get("version_number", ""))
    log(title)
    for dep in ver.get("dependencies", []):
        if dep.get("dependency_type") != "required":
            continue
        dep_id = dep.get("project_id")
        if not dep_id and dep.get("version_id"):
            vid = urllib.parse.quote(str(dep["version_id"]), safe="")
            dep_id = get_json(f"{MODRINTH}/version/{vid}").get("project_id")
        if dep_id:
            modrinth_install(dep_id, game_version, game_dir, log, None, seen, loader)


def curseforge_get(path: str, api_key: str):
    if not api_key:
        raise LauncherError("Brak klucza API CurseForge. Wpisz go w Ustawieniach.")
    _cdn_key["value"] = api_key
    try:
        return get_json(CURSEFORGE + path, headers={"x-api-key": api_key})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LauncherError("CurseForge odrzucił klucz API (sprawdź go w Ustawieniach)")
        raise


def curseforge_search(query: str, game_version: str, api_key: str, limit: int = 30, offset: int = 0) -> list:
    """Bez zapytania zwraca najpopularniejsze mody."""
    params = {"gameId": 432, "classId": 6, "modLoaderType": 4, "gameVersion": game_version,
              "pageSize": limit, "index": offset}
    if query:
        params["searchFilter"] = query
    else:
        params.update({"sortField": 2, "sortOrder": "desc"})  # popularnosc
    data = curseforge_get(f"/mods/search?{urllib.parse.urlencode(params)}", api_key).get("data", [])
    return [_curseforge_hit(m) for m in data]


def curseforge_install(mod_id, game_version, game_dir, api_key, log=print, title=None, _seen=None,
                       loader="fabric") -> None:
    seen = _seen if _seen is not None else set()
    mod_id = int(mod_id)
    if mod_id in seen:
        return
    seen.add(mod_id)
    if title is None:
        title = curseforge_get(f"/mods/{mod_id}", api_key).get("data", {}).get("name", str(mod_id))
    query = urllib.parse.urlencode({"gameVersion": game_version, "modLoaderType": CURSEFORGE_LOADER[loader],
                                    "pageSize": 20})
    files = curseforge_get(f"/mods/{mod_id}/files?{query}", api_key).get("data", [])
    if not files:
        raise LauncherError(f"„{title}” nie ma wersji dla Minecraft {game_version} z {LOADER_LABELS[loader]}")
    f = sorted(files, key=lambda x: x.get("fileDate", ""), reverse=True)[0]
    if not f.get("downloadUrl"):
        raise LauncherError(f"Autor „{title}” nie pozwala pobierać moda przez zewnętrzne launchery. "
                            "Pobierz plik ze strony CurseForge i wrzuć go do folderu mods.")
    sha1 = next((h.get("value") for h in f.get("hashes", []) if h.get("algo") == 1), None)
    _store_mod(game_dir, f["downloadUrl"], f["fileName"], sha1, f.get("fileLength"),
               "curseforge", mod_id, title, f.get("displayName", ""))
    log(title)
    for dep in f.get("dependencies", []):
        if dep.get("relationType") == 3 and dep.get("modId"):  # 3 = wymagana zaleznosc
            curseforge_install(dep["modId"], game_version, game_dir, api_key, log, None, seen, loader)


def install_mod(hit: dict, game_version: str, game_dir: Path, curseforge_key: str = "", log=print,
                loader: str = "fabric") -> None:
    if loader not in MODRINTH_LOADERS:
        raise LauncherError("Ten profil nie ma loadera modów. Wybierz Fabric, Quilt, Forge albo NeoForge.")
    if hit["source"] == "curseforge":
        curseforge_install(hit["id"], game_version, game_dir, curseforge_key, log, hit.get("title"), None, loader)
    else:
        modrinth_install(hit["id"], game_version, game_dir, log, hit.get("title"), None, loader)


# ------------------------------------------------- typy tresci, szczegoly, modpacki

CONTENT_TYPES = {
    "mod": {"label": "Mody", "mr": [["project_type:mod"], ["categories:fabric"]], "mr_loaders": ["fabric"],
            "cf_slug": "mc-mods", "cf_class": 6, "cf_loader": 4},
    "modpack": {"label": "Modpacki", "mr": [["project_type:modpack"]],
                "mr_loaders": ["fabric", "quilt", "forge", "neoforge"], "cf_slug": "modpacks", "cf_class": 4471,
                "cf_loader": None},
    "resourcepack": {"label": "Paczki zasobów", "mr": [["project_type:resourcepack"]], "mr_loaders": ["minecraft"],
                     "cf_slug": "texture-packs", "cf_class": 12, "cf_loader": None},
    "datapack": {"label": "Paczki danych", "mr": [["categories:datapack"]], "mr_loaders": ["datapack"],
                 "cf_slug": "data-packs", "cf_class": None, "cf_loader": None},  # id klasy pobierane z API
}
_cf_classes = {}


def curseforge_post(path: str, payload: dict, api_key: str):
    if not api_key:
        raise LauncherError("Brak klucza API CurseForge. Wpisz go w Ustawieniach.")
    _cdn_key["value"] = api_key
    try:
        return get_json(CURSEFORGE + path, data=payload, headers={"x-api-key": api_key})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LauncherError("CurseForge odrzucił klucz API (sprawdź go w Ustawieniach)")
        raise


def curseforge_class_id(kind: str, api_key: str) -> int:
    t = CONTENT_TYPES[kind]
    if t["cf_class"]:
        return t["cf_class"]
    if t["cf_slug"] not in _cf_classes:
        for c in curseforge_get("/categories?gameId=432&classesOnly=true", api_key).get("data", []):
            if c.get("slug"):
                _cf_classes[c["slug"]] = c["id"]
    if t["cf_slug"] not in _cf_classes:
        raise LauncherError(f"CurseForge nie ma kategorii „{t['label']}” dla Minecrafta")
    return _cf_classes[t["cf_slug"]]


def search_content(kind, source, query, game_version, api_key="", limit=30, offset=0, sort=None,
                   loader="fabric") -> list:
    """Wyszukiwanie dowolnego typu tresci. Modpacki nie sa filtrowane po wersji gry ani loaderze."""
    version = None if kind == "modpack" else game_version
    if kind == "mod" and loader not in MODRINTH_LOADERS:
        raise LauncherError("Ten profil nie ma loadera modów. Wybierz go w edycji profilu.")
    if source == "modrinth":
        if kind == "mod":
            return modrinth_search(query, version, limit, offset, sort, loader)
        facets = [list(f) for f in CONTENT_TYPES[kind]["mr"]] + ([[f"versions:{version}"]] if version else [])
        params = {"limit": limit, "offset": offset, "facets": json.dumps(facets),
                  "index": sort or ("relevance" if query else "downloads")}
        if query:
            params["query"] = query
        hits = get_json(f"{MODRINTH}/search?{urllib.parse.urlencode(params)}").get("hits", [])
        return [_modrinth_hit(h, kind) for h in hits]
    t = CONTENT_TYPES[kind]
    params = {"gameId": 432, "classId": curseforge_class_id(kind, api_key), "pageSize": limit, "index": offset}
    if kind == "mod":
        params["modLoaderType"] = CURSEFORGE_LOADER[loader]
    elif t["cf_loader"]:
        params["modLoaderType"] = t["cf_loader"]
    if version:
        params["gameVersion"] = version
    if query:
        params["searchFilter"] = query
    else:
        params.update({"sortField": 2, "sortOrder": "desc"})  # popularnosc
    data = curseforge_get(f"/mods/search?{urllib.parse.urlencode(params)}", api_key).get("data", [])
    return [_curseforge_hit(m) for m in data]


def _modrinth_hit(h: dict, kind: str = "mod") -> dict:
    slug = h.get("slug") or h["project_id"]
    page = {"modpack": "modpack", "resourcepack": "resourcepack", "datapack": "datapack"}.get(kind, "mod")
    return {"source": "modrinth", "id": h["project_id"], "title": h.get("title", h["project_id"]),
            "description": h.get("description", ""), "downloads": int(h.get("downloads", 0)),
            "author": h.get("author", ""), "url": f"https://modrinth.com/{page}/{slug}",
            "icon": h.get("icon_url") or ""}


def _curseforge_hit(m: dict) -> dict:
    return {"source": "curseforge", "id": str(m["id"]), "title": m.get("name", str(m["id"])),
            "description": m.get("summary", ""), "downloads": int(m.get("downloadCount", 0)),
            "author": ((m.get("authors") or [{}])[0] or {}).get("name", ""),
            "url": (m.get("links") or {}).get("websiteUrl", ""),
            "icon": (m.get("logo") or {}).get("thumbnailUrl", "")}


def plain_text(text: str) -> str:
    """Zamienia opis w Markdownie (Modrinth) albo HTML (CurseForge) na zwykly tekst."""
    text = str(text or "")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>|</tr>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)        # obrazki
    text = re.sub(r"\[\]\([^)]*\)", "", text)                # puste linki po obrazkach
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)    # linki -> sam tekst
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)        # naglowki
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)           # listy
    text = re.sub(r"(\*\*|__|`|~~)", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def project_details(hit: dict, api_key: str = "") -> dict:
    """Pelny opis, linki, kategorie i data aktualizacji projektu."""
    if hit["source"] == "modrinth":
        p = get_json(f"{MODRINTH}/project/{urllib.parse.quote(str(hit['id']), safe='')}")
        lic = p.get("license") or {}
        links = [("Strona projektu", hit.get("url")), ("Kod źródłowy", p.get("source_url")),
                 ("Zgłoś błąd", p.get("issues_url")), ("Wiki", p.get("wiki_url")), ("Discord", p.get("discord_url"))]
        return {"description": plain_text(p.get("body") or p.get("description", "")),
                "categories": [c for c in p.get("categories", [])
                               if c not in ("fabric", "quilt", "forge", "neoforge", "datapack", "minecraft")],
                "updated": str(p.get("updated") or "")[:10], "license": lic.get("name") or lic.get("id") or "",
                "followers": p.get("followers"),
                "links": [(label, url) for label, url in links if str(url or "").startswith("https://")]}
    mod_id = int(hit["id"])
    data = curseforge_get(f"/mods/{mod_id}", api_key).get("data", {})
    try:
        description = curseforge_get(f"/mods/{mod_id}/description", api_key).get("data", "")
    except (urllib.error.HTTPError, LauncherError):
        description = data.get("summary", "")
    links = data.get("links") or {}
    pairs = [("Strona projektu", links.get("websiteUrl") or hit.get("url")), ("Kod źródłowy", links.get("sourceUrl")),
             ("Zgłoś błąd", links.get("issuesUrl")), ("Wiki", links.get("wikiUrl"))]
    return {"description": plain_text(description or data.get("summary", "")),
            "categories": [c.get("name", "") for c in data.get("categories", []) if c.get("name")],
            "updated": str(data.get("dateModified") or "")[:10], "license": "", "followers": None,
            "links": [(label, url) for label, url in pairs if str(url or "").startswith("https://")]}


def fetch_icon(url: str) -> bytes:
    """Ikona projektu, tylko PNG lub GIF (tyle obsluguje Tk bez dodatkowych bibliotek)."""
    if not str(url).startswith("https://"):
        return b""
    data = http(url, headers={"Accept": "image/png,image/gif,*/*"}, timeout=15)
    if len(data) > 600_000 or not (data.startswith(b"\x89PNG") or data.startswith(b"GIF8")):
        return b""
    return data


def safe_join(base: Path, rel: str) -> Path:
    """Sciezka z paczki, ktora nie moze wyjsc poza folder profilu."""
    rel = str(rel).replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if (not parts or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel)
            or any(p == ".." or ":" in p for p in parts)):
        raise LauncherError(f"Niebezpieczna ścieżka w paczce: {rel}")
    target = base.joinpath(*parts)
    base_resolved = base.resolve()
    if base_resolved != target.resolve() and base_resolved not in target.resolve().parents:
        raise LauncherError(f"Niebezpieczna ścieżka w paczce: {rel}")
    return target


def _extract_overrides(zf: zipfile.ZipFile, prefix: str, game_dir: Path) -> int:
    prefix = prefix.strip("/") + "/"
    count = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(prefix) or info.filename == prefix:
            continue
        target = safe_join(game_dir, info.filename[len(prefix):])
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    return count


def _install_mrpack(zf, game_dir, progress=None) -> dict:
    index = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
    if index.get("game", "minecraft") != "minecraft":
        raise LauncherError("To nie jest modpack do Minecrafta")
    deps = index.get("dependencies") or {}
    if not deps.get("minecraft"):
        raise LauncherError("Modpack nie podaje wersji Minecrafta")
    loader, loader_version = "vanilla", ""
    for key, name in (("fabric-loader", "fabric"), ("quilt-loader", "quilt"), ("forge", "forge"),
                      ("neoforge", "neoforge")):
        if deps.get(key):
            loader, loader_version = name, str(deps[key])
            break
    tasks = []
    for f in index.get("files", []):
        if (f.get("env") or {}).get("client") == "unsupported":
            continue  # plik tylko dla serwera
        urls = [u for u in f.get("downloads", []) if str(u).startswith("https://")]
        if not urls:
            raise LauncherError(f"Brak bezpiecznego adresu pobierania dla {f.get('path')}")
        tasks.append((urls[0], safe_join(game_dir, f["path"]), (f.get("hashes") or {}).get("sha1"), f.get("fileSize")))
    download_all(tasks, progress)
    _extract_overrides(zf, "overrides", game_dir)
    _extract_overrides(zf, "client-overrides", game_dir)
    return {"name": index.get("name") or "Modpack", "game_version": deps["minecraft"],
            "loader": loader, "loader_version": loader_version, "version": index.get("versionId", ""),
            "files": len(tasks), "blocked": []}


def _install_cf_manifest(zf, game_dir, api_key, progress=None) -> dict:
    m = json.loads(zf.read("manifest.json").decode("utf-8"))
    mc = (m.get("minecraft") or {}).get("version")
    entries = (m.get("minecraft") or {}).get("modLoaders", [])
    primary = next((x for x in entries if x.get("primary")), entries[0] if entries else {})
    loader_id = str(primary.get("id", ""))
    if not mc:
        raise LauncherError("Modpack nie podaje wersji Minecrafta")
    loader, loader_version = "vanilla", ""
    if loader_id:
        name, _, loader_version = loader_id.partition("-")
        if name not in MODRINTH_LOADERS:
            raise LauncherError(f"Ten modpack wymaga loadera {loader_id}, którego Zorza nie obsługuje")
        loader = name
    file_ids = [int(f["fileID"]) for f in m.get("files", []) if f.get("required", True)]
    files = curseforge_post("/mods/files", {"fileIds": file_ids}, api_key).get("data", []) if file_ids else []
    mod_ids = sorted({f["modId"] for f in files})
    mods = {d["id"]: d for d in curseforge_post("/mods", {"modIds": mod_ids}, api_key).get("data", [])} if mod_ids else {}
    folders = {12: "resourcepacks", 6552: "shaderpacks"}
    tasks, blocked = [], []
    for f in files:
        mod = mods.get(f["modId"], {})
        folder = folders.get(mod.get("classId"), "mods" if str(f.get("fileName", "")).endswith(".jar") else "resourcepacks")
        name = safe_file_name(f.get("fileName", ""), (".jar", ".zip"))
        if not str(f.get("downloadUrl") or "").startswith("https://"):
            blocked.append({"name": mod.get("name") or name, "file": name, "folder": folder,
                            "url": (mod.get("links") or {}).get("websiteUrl", "")})
            continue
        sha1 = next((h.get("value") for h in f.get("hashes", []) if h.get("algo") == 1), None)
        tasks.append((f["downloadUrl"], game_dir / folder / name, sha1, f.get("fileLength")))
    download_all(tasks, progress)
    _extract_overrides(zf, m.get("overrides") or "overrides", game_dir)
    return {"name": m.get("name") or "Modpack", "game_version": mc, "loader": loader, "loader_version": loader_version,
            "version": m.get("version", ""), "files": len(tasks), "blocked": blocked,
            "missing": max(0, len(file_ids) - len(files))}


def install_modpack_file(path: Path, game_dir: Path, api_key: str = "", progress=None) -> dict:
    """Instaluje modpack z pliku .mrpack (Modrinth) albo .zip (CurseForge) do nowego folderu profilu."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise LauncherError("To nie jest prawidłowy plik modpacka")
    with zf:
        names = set(zf.namelist())
        game_dir.mkdir(parents=True, exist_ok=True)
        if "modrinth.index.json" in names:
            return _install_mrpack(zf, game_dir, progress)
        if "manifest.json" in names:
            return _install_cf_manifest(zf, game_dir, api_key, progress)
    raise LauncherError("Plik nie zawiera ani modrinth.index.json, ani manifest.json")


def modrinth_pick(project_id, loaders, game_version=None):
    params = {"loaders": json.dumps(loaders)}
    if game_version:
        params["game_versions"] = json.dumps([game_version])
    pid = urllib.parse.quote(str(project_id), safe="")
    versions = get_json(f"{MODRINTH}/project/{pid}/version?{urllib.parse.urlencode(params)}")
    return versions[0] if versions else None


def curseforge_pick_file(mod_id, game_version, loader_type, api_key):
    params = {"pageSize": 50}
    if game_version:
        params["gameVersion"] = game_version
    if loader_type:
        params["modLoaderType"] = loader_type
    files = curseforge_get(f"/mods/{int(mod_id)}/files?{urllib.parse.urlencode(params)}", api_key).get("data", [])
    return sorted(files, key=lambda x: x.get("fileDate", ""), reverse=True)[0] if files else None


def _primary_file(version: dict, title: str) -> dict:
    files = version.get("files") or []
    if not files:
        raise LauncherError(f"„{title}” nie ma pliku do pobrania")
    return next((x for x in files if x.get("primary")), files[0])


def install_modpack(hit: dict, game_dir: Path, api_key: str = "", progress=None) -> dict:
    title = hit.get("title", str(hit["id"]))
    cache = ROOT / "cache"
    if hit["source"] == "modrinth":
        ver = modrinth_pick(hit["id"], CONTENT_TYPES["modpack"]["mr_loaders"])
        if not ver:
            raise LauncherError(f"„{title}” nie ma wersji do pobrania")
        f = _primary_file(ver, title)
        url, name, sha1, size = f["url"], safe_file_name(f["filename"], (".mrpack",)), f.get("hashes", {}).get("sha1"), f.get("size")
    else:
        f = curseforge_pick_file(hit["id"], None, None, api_key)
        if not f:
            raise LauncherError(f"„{title}” nie ma wersji do pobrania")
        if not f.get("downloadUrl"):
            raise LauncherError(f"Autor „{title}” nie pozwala pobierać modpacka przez zewnętrzne launchery.")
        url, name, size = f["downloadUrl"], safe_file_name(f["fileName"], (".zip",)), f.get("fileLength")
        sha1 = next((h.get("value") for h in f.get("hashes", []) if h.get("algo") == 1), None)
    if not str(url).startswith("https://"):
        raise LauncherError("Odrzucono adres pobierania, który nie używa HTTPS")
    download(url, cache / name, sha1, size)
    return install_modpack_file(cache / name, game_dir, api_key, progress)


def install_pack(hit: dict, kind: str, game_version: str, target_dir: Path, index_path: Path,
                 api_key: str = "", log=print) -> None:
    """Paczka zasobow albo paczka danych: jeden plik .zip, bez zaleznosci."""
    title = hit.get("title", str(hit["id"]))
    if hit["source"] == "modrinth":
        ver = modrinth_pick(hit["id"], CONTENT_TYPES[kind]["mr_loaders"], game_version)
        if not ver:
            raise LauncherError(f"„{title}” nie ma wersji dla Minecraft {game_version}")
        f = _primary_file(ver, title)
        _store_file(target_dir, index_path, f["url"], f["filename"], (".zip",), f.get("hashes", {}).get("sha1"),
                    f.get("size"), "modrinth", hit["id"], title, ver.get("version_number", ""))
    else:
        f = curseforge_pick_file(hit["id"], game_version, None, api_key)
        if not f:
            raise LauncherError(f"„{title}” nie ma wersji dla Minecraft {game_version}")
        if not f.get("downloadUrl"):
            raise LauncherError(f"Autor „{title}” nie pozwala pobierać przez zewnętrzne launchery. "
                                "Pobierz plik ze strony CurseForge.")
        sha1 = next((h.get("value") for h in f.get("hashes", []) if h.get("algo") == 1), None)
        _store_file(target_dir, index_path, f["downloadUrl"], f["fileName"], (".zip",), sha1, f.get("fileLength"),
                    "curseforge", hit["id"], title, f.get("displayName", ""))
    log(title)


def list_worlds(game_dir: Path) -> list:
    saves = game_dir / "saves"
    if not saves.is_dir():
        return []
    worlds = [p for p in saves.iterdir() if p.is_dir() and (p / "level.dat").exists()]
    return sorted(worlds, key=lambda p: (p / "level.dat").stat().st_mtime, reverse=True)


def list_packs(folder: Path, index_path: Path) -> list:
    """Paczki zasobow lub danych: pliki .zip i rozpakowane foldery."""
    index = _load_index(index_path)
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
        if p.is_dir() or p.name.lower().endswith(".zip"):
            info = index.get(p.name, {})
            out.append({"file": p.name, "title": info.get("title", p.name), "source": info.get("source", "ręcznie"),
                        "folder": p.is_dir()})
    return out


def remove_pack(folder: Path, index_path: Path, name: str) -> None:
    name = str(name)
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise LauncherError(f"Nieprawidłowa nazwa: {name}")
    path = folder / name
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    index = _load_index(index_path)
    if index.pop(name, None) is not None:
        _save_index(index_path, index)


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if OS_NAME == "windows":
        os.startfile(str(path))  # noqa: tylko Windows
    else:
        subprocess.Popen(["open" if OS_NAME == "osx" else "xdg-open", str(path)])


def format_count(n: int) -> str:
    for limit, suffix in ((1_000_000, " mln"), (1_000, " tys.")):
        if n >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".").replace(".", ",") + suffix
    return str(n)


def plural_mods(n: int) -> str:
    if n == 1:
        return "1 mod"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} mody"
    return f"{n} modów"


def shorten(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def tile_color(key: str) -> str:
    palette = ("#3ddc97", "#2bc4c9", "#8b5cf6", "#d16ba5", "#f2a65a", "#5b8def")
    return palette[sum(map(ord, key)) % len(palette)]


def launch_settings(cfg: dict, profile: dict) -> dict:
    """Ustawienia globalne nadpisane ustawieniami profilu."""
    loader = profile.get("loader") if profile.get("loader") in LOADERS else (
        "fabric" if profile.get("use_fabric", True) else "vanilla")
    return dict(cfg, game_version=profile["game_version"], loader=loader,
                loader_version=profile.get("loader_version") or profile.get("fabric_loader", ""),
                memory_mb=int(profile.get("memory_mb") or cfg.get("memory_mb", 4096)),
                jvm_args=profile.get("jvm_args", ""))


# ---------------------------------------------------------------- interfejsy

def run_cli():
    try:
        sys.stdout.reconfigure(errors="replace")  # konsola Windows bez pelnego Unicode
    except AttributeError:
        pass
    cfg = load_config()
    auth = MicrosoftAuth(cfg["msa_client_id"])
    account = auth.login(lambda code, uri: print(f"Wejdz na {uri} i wpisz kod: {code}"))
    print(f"Zalogowano: {account['name']}")

    def progress(done, total):
        print(f"\r  {done}/{total}", end="" if done < total else "\n", flush=True)

    pdata = load_profiles(cfg)
    p = pdata["profiles"][pdata["selected"]]
    print(f"Profil: {p['name']}")
    proc = prepare_and_launch(launch_settings(cfg, p), account, print, progress, profile_dir(pdata["selected"]))
    for line in proc.stdout:
        print(line, end="")
    sys.exit(proc.wait())


def render_aurora(w, h, bg=(11, 13, 26)):
    """Rysuje zorze polarna piksel po pikselu. Zwraca wiersze kolorow '#rrggbb'."""
    rnd = random.Random(7)
    top = (4, 6, 16)
    ribbons = [  # kolor, wysokosc (ulamek h), amplituda, czestotliwosc, faza, dlugosc smugi
        ((61, 220, 151), 0.74, 0.07, 0.0105, 0.4, 0.26),
        ((43, 196, 201), 0.62, 0.06, 0.0160, 2.1, 0.20),
        ((139, 92, 246), 0.50, 0.08, 0.0085, 4.0, 0.22),
    ]
    sx = 380.0 / w  # ksztalt nie zalezy od rozdzielczosci
    px = []
    for y in range(h):
        t = y / max(1, h - 1)
        base = tuple(top[i] + (bg[i] - top[i]) * t for i in range(3))
        px.append([list(base) for _ in range(w)])
    for _ in range(int(w * h / 1400)):  # gwiazdy
        x, y, b = rnd.randrange(w), rnd.randrange(int(h * 0.7)), rnd.randint(50, 170)
        for i in range(3):
            px[y][x][i] += b
    fade = [min(1.0, (h - y) / (h * 0.22)) * min(1.0, 0.15 + y / (h * 0.30)) for y in range(h)]
    for color, y0, amp, freq, phase, length in ribbons:
        L = max(2.0, length * h)
        up = [math.exp(-d / L) for d in range(h + 1)]
        down = [math.exp(-d / (0.03 * h)) for d in range(h + 1)]
        for x in range(w):
            u = x * sx
            c = h * (y0 + amp * math.sin(u * freq + phase) + amp * 0.35 * math.sin(u * freq * 2.7 + phase * 1.9))
            ci = int(c)
            streak = 0.52 + 0.20 * math.sin(u * 0.045 + phase * 3) + 0.10 * math.sin(u * 0.17 + phase) + 0.05 * math.sin(u * 0.41)
            for y in range(max(0, int(c - 4 * L)), min(h, ci + int(0.12 * h))):
                k = (up[ci - y] if y <= ci else down[y - ci]) * streak * fade[y]
                if k > 0.004:
                    cell = px[y][x]
                    cell[0] += color[0] * k
                    cell[1] += color[1] * k
                    cell[2] += color[2] * k
    return [["#%02x%02x%02x" % tuple(min(255, max(0, int(v))) for v in cell) for cell in row] for row in px]


def round_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
            x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    BG, PANEL, PANEL2, TEXT, MUTED, LINE = "#0b0d1a", "#11142a", "#1a1f3d", "#e9ecff", "#8088a8", "#262b4d"
    GREEN, GREEN_HI, RED, SELECT = "#3ddc97", "#6ff0b6", "#ff7a90", "#2b3f78"
    SORTS = (("Popularność", "downloads"), ("Trafność", "relevance"),
             ("Ostatnio zaktualizowane", "updated"), ("Najnowsze", "newest"))
    PAGE, TILE_W, TILE_H = 30, 136, 140

    def font(px, *style):
        return ("Segoe UI", -px) + style  # ujemny rozmiar = piksele, uklad nie zalezy od DPI

    cfg = load_config()
    pdata = load_profiles(cfg)
    events = queue.Queue()
    state = {"account": None, "login": None, "preparing": None, "running": None, "play_after": None,
             "versions": ["latest"], "log": [], "console": None, "editors": {}, "downloaders": {},
             "modpack_busy": False}

    root = tk.Tk()
    root.title(f"{NAME} {VERSION}")
    root.configure(bg=BG)
    root.minsize(860, 540)
    W, H = 1000, 640
    root.geometry(f"{W}x{H}+{max(0, (root.winfo_screenwidth() - W) // 2)}"
                  f"+{max(0, (root.winfo_screenheight() - H) // 3)}")

    try:  # ciemny wyglad list rozwijanych i pasków przewijania
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL2, foreground=TEXT,
                        arrowcolor=TEXT, bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL), ("disabled", BG)],
                  foreground=[("readonly", TEXT), ("disabled", MUTED)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)])
        style.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=BG, bordercolor=BG,
                        arrowcolor=MUTED, lightcolor=PANEL2, darkcolor=PANEL2)
        for key, value in (("background", PANEL), ("foreground", TEXT),
                           ("selectBackground", SELECT), ("selectForeground", TEXT)):
            root.option_add(f"*TCombobox*Listbox.{key}", value)
    except tk.TclError:
        pass

    # ================================================================ pomocnicze
    def ui(fn):
        events.put(fn)  # wywolane w glownym watku okna

    def background(job, done, fail):
        """Uruchamia job() w osobnym watku; wynik albo blad wraca do okna."""
        def run():
            try:
                result = job()
            except LauncherError as e:
                ui(lambda m=str(e): fail(m))
            except Exception as e:  # siec, nieoczekiwane odpowiedzi API
                ui(lambda m=f"{type(e).__name__}: {e}": fail(m))
            else:
                ui(lambda: done(result))
        threading.Thread(target=run, daemon=True).start()

    def alive(win):
        try:
            return bool(win.winfo_exists())
        except tk.TclError:
            return False

    def make_button(parent, text, command, kind="normal"):
        bg, fg, active = {"normal": (PANEL2, TEXT, SELECT), "primary": (GREEN, "#06281b", GREEN_HI),
                          "danger": (PANEL2, RED, "#3a2134")}[kind]
        return tk.Button(parent, text=text, command=command, relief="flat", bd=0, padx=14, pady=6,
                         cursor="hand2", font=font(13, "bold") if kind == "primary" else font(13),
                         bg=bg, fg=fg, activebackground=active, activeforeground=fg,
                         disabledforeground="#59607f", highlightthickness=0)

    def make_entry(parent, **kw):
        return tk.Entry(parent, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", font=font(13),
                        highlightthickness=1, highlightbackground=LINE, highlightcolor=GREEN, **kw)

    def make_label(parent, text="", size=13, color=TEXT, bold=False, bg=BG, anchor="w", **kw):
        return tk.Label(parent, text=text, bg=bg, fg=color, anchor=anchor, justify="left",
                        font=font(size, "bold") if bold else font(size), **kw)

    def make_listbox(parent, height=10):
        return tk.Listbox(parent, height=height, bg=PANEL, fg=TEXT, selectbackground=SELECT,
                          selectforeground=TEXT, relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=LINE, highlightcolor=LINE, activestyle="none",
                          font=font(13), exportselection=False)

    def make_combobox(parent, values, readonly=False):
        return ttk.Combobox(parent, values=list(values), font=font(13), state="readonly" if readonly else "normal")

    def make_check(parent, text, var, bg=BG):
        return tk.Checkbutton(parent, text=text, variable=var, bg=bg, fg=TEXT, selectcolor=PANEL,
                              activebackground=bg, activeforeground=TEXT, highlightthickness=0,
                              font=font(13), anchor="w")

    def make_menu():
        return tk.Menu(root, tearoff=0, bg=PANEL, fg=TEXT, activebackground=SELECT,
                       activeforeground=TEXT, disabledforeground=MUTED, bd=0)

    def make_dialog(title, w, h, resizable=False):
        d = tk.Toplevel(root)
        d.title(title)
        d.configure(bg=BG)
        d.transient(root)
        x = root.winfo_rootx() + max(0, (root.winfo_width() - w) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - h) // 3)
        d.geometry(f"{w}x{h}+{x}+{y}")
        d.resizable(resizable, resizable)
        return d

    def form_row(parent, row, label, widget):
        make_label(parent, label, 13, MUTED).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=5)
        widget.grid(row=row, column=1, sticky="we", pady=5, ipady=3)

    def popup(menu, widget):
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def copy(text):
        root.clipboard_clear()
        root.clipboard_append(text)

    def profile(pid=None):
        return pdata["profiles"][pid or pdata["selected"]]

    def version_label(p):
        version = "najnowsza" if p["game_version"] == "latest" else p["game_version"]
        loader = p.get("loader", "fabric")
        return f"Minecraft {version}" + (f" · {LOADER_LABELS[loader]}" if loader in MODRINTH_LOADERS else "")

    def has_mods(p):
        return p.get("loader", "fabric") in MODRINTH_LOADERS

    loader_names = [LOADER_LABELS[x] for x in LOADERS]

    def loader_from_label(label):
        return next((k for k, v in LOADER_LABELS.items() if v == label), "vanilla")

    def is_active(pid):
        return state["preparing"] == pid or bool(state["running"] and state["running"]["pid"] == pid)

    def write_log(msg):
        state["log"].append(str(msg))
        del state["log"][:-5000]
        console = state["console"]
        if console and alive(console["win"]):
            console["text"].insert("end", str(msg) + "\n")
            console["text"].see("end")

    # ======================================================= okno: baner i pasek
    banner = tk.Canvas(root, height=64, bg=BG, highlightthickness=0, bd=0)
    banner.pack(fill="x")
    banner_w = max(1400, min(7680, root.winfo_screenwidth() + 200))  # caly ekran, takze 4K i dwa monitory
    try:
        rows = render_aurora(banner_w, 64)
        root.aurora = tk.PhotoImage(width=banner_w, height=64)  # referencja, zeby obraz nie zniknal
        root.aurora.put(" ".join("{" + " ".join(r) + "}" for r in rows))
        banner.create_image(0, 0, image=root.aurora, anchor="nw")
    except tk.TclError:
        pass
    logo = banner.create_text(24, 32, anchor="w", text="ZORZA", font=font(28, "bold"), fill="#ffffff")
    box = banner.bbox(logo)
    banner.create_text((box[2] if box else 130) + 10, 36, anchor="w", text="CLIENT", font=font(12), fill="#b8f5da")

    toolbar = tk.Frame(root, bg=PANEL)
    toolbar.pack(fill="x")
    make_button(toolbar, "＋  Dodaj profil", lambda: add_profile_dialog()).pack(side="left", padx=(10, 4), pady=6)
    make_button(toolbar, "Modpacki…", lambda: open_downloader(kind="modpack")).pack(side="left", padx=4)
    make_button(toolbar, "Importuj modpack…", lambda: import_modpack()).pack(side="left", padx=4)
    folders_btn = make_button(toolbar, "Foldery  ▾", lambda: popup(folders_menu(), folders_btn))
    folders_btn.pack(side="left", padx=4)
    make_button(toolbar, "Ustawienia", lambda: settings_dialog()).pack(side="left", padx=4)
    make_button(toolbar, "Konsola", lambda: open_console()).pack(side="left", padx=4)
    account_btn = make_button(toolbar, "Zaloguj się  ▾", lambda: popup(account_menu(), account_btn))
    account_btn.pack(side="right", padx=10)

    statusbar = tk.Frame(root, bg=PANEL)
    statusbar.pack(side="bottom", fill="x")
    status_lbl = make_label(statusbar, "Gotowe", 12, MUTED, bg=PANEL)
    status_lbl.pack(side="left", padx=12, pady=5)
    prog = tk.Canvas(statusbar, width=220, height=6, bg=LINE, highlightthickness=0, bd=0)
    prog.pack(side="right", padx=12)
    prog_bar = prog.create_rectangle(0, 0, 0, 6, fill=GREEN, outline="")

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True)
    side = tk.Frame(body, bg=PANEL, width=250)
    side.pack(side="right", fill="y")
    side.pack_propagate(False)
    grid = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
    grid_scroll = ttk.Scrollbar(body, orient="vertical", command=grid.yview)
    grid.configure(yscrollcommand=grid_scroll.set)
    grid_scroll.pack(side="right", fill="y")
    grid.pack(side="left", fill="both", expand=True)

    def set_status(msg, error=False):
        msg = str(msg)
        status_lbl.config(text=shorten(msg.splitlines()[0] if msg else "", 120), fg=RED if error else MUTED)
        write_log(("BŁĄD: " if error else "") + msg)

    def set_progress(frac):
        prog.coords(prog_bar, 0, 0, 220 * max(0.0, min(1.0, frac)), 6)

    # ============================================================ kafelki profili
    def draw_tiles(_event=None):
        grid.delete("all")
        width = max(grid.winfo_width(), TILE_W + 32)
        cols = max(1, (width - 32) // TILE_W)
        for i, (pid, p) in enumerate(pdata["profiles"].items()):
            r, c = divmod(i, cols)
            x0, y0 = 16 + c * TILE_W, 16 + r * TILE_H
            cx, tag = x0 + TILE_W // 2, "tile_" + pid
            if pid == pdata["selected"]:
                grid.create_polygon(round_points(x0 + 4, y0 + 2, x0 + TILE_W - 4, y0 + TILE_H - 6, 14),
                                    smooth=True, fill=SELECT, outline="", tags=(tag,))
            grid.create_polygon(round_points(cx - 32, y0 + 14, cx + 32, y0 + 78, 16), smooth=True,
                                fill=tile_color(pid), outline="", tags=(tag,))
            grid.create_text(cx, y0 + 46, text=(p["name"][:1] or "?").upper(), font=font(30, "bold"),
                             fill=BG, tags=(tag,))
            grid.create_text(cx, y0 + 96, text=shorten(p["name"], 16), font=font(13, "bold"), fill=TEXT,
                             tags=(tag,))
            running = is_active(pid)
            sub = ("▶ w grze" if state["running"] else "uruchamianie…") if running else version_label(p).replace("Minecraft ", "")
            grid.create_text(cx, y0 + 116, text=shorten(sub, 20), font=font(11), fill=GREEN if running else MUTED,
                             tags=(tag,))
            grid.tag_bind(tag, "<Button-1>", lambda e, pid=pid: select_profile(pid))
            grid.tag_bind(tag, "<Double-Button-1>", lambda e, pid=pid: (select_profile(pid), play(pid)))
            grid.tag_bind(tag, "<Button-3>", lambda e, pid=pid: (select_profile(pid), context_menu(e)))
        rows_count = (len(pdata["profiles"]) + cols - 1) // cols
        grid.configure(scrollregion=(0, 0, width, 32 + rows_count * TILE_H))

    grid.bind("<Configure>", draw_tiles)
    grid.bind("<MouseWheel>", lambda e: grid.yview_scroll(int(-e.delta / 120), "units"))

    # ================================================================ panel boczny
    side_icon = tk.Canvas(side, width=84, height=84, bg=PANEL, highlightthickness=0, bd=0)
    side_icon.pack(pady=(22, 8))
    side_name = make_label(side, "", 16, TEXT, bold=True, bg=PANEL, anchor="center")
    side_name.pack(fill="x", padx=12)
    side_ver = make_label(side, "", 12, MUTED, bg=PANEL, anchor="center")
    side_ver.pack(fill="x", padx=12)
    side_mods = make_label(side, "", 12, MUTED, bg=PANEL, anchor="center")
    side_mods.pack(fill="x", padx=12, pady=(0, 14))
    play_btn = make_button(side, "▶  Graj", lambda: play(), "primary")
    play_btn.pack(fill="x", padx=18, pady=(0, 6), ipady=4)
    stop_btn = make_button(side, "■  Zatrzymaj", lambda: stop_game(), "danger")
    stop_btn.pack(fill="x", padx=18, pady=(0, 14))
    for text, command in (("Edytuj profil…", lambda: edit_profile()),
                          ("Pobierz mody i paczki…", lambda: open_downloader()),
                          ("Folder profilu", lambda: open_folder(profile_dir(pdata["selected"]))),
                          ("Kopiuj profil", lambda: copy_profile())):
        make_button(side, text, command).pack(fill="x", padx=18, pady=3)
    delete_btn = make_button(side, "Usuń profil", lambda: delete_profile(), "danger")
    delete_btn.pack(fill="x", padx=18, pady=3)

    def refresh_side():
        pid, p = pdata["selected"], profile()
        side_icon.delete("all")
        side_icon.create_polygon(round_points(6, 6, 78, 78, 18), smooth=True, fill=tile_color(pid), outline="")
        side_icon.create_text(42, 42, text=(p["name"][:1] or "?").upper(), font=font(36, "bold"), fill=BG)
        side_name.config(text=shorten(p["name"], 22))
        side_ver.config(text=version_label(p))
        side_mods.config(text=plural_mods(len(list_installed(profile_dir(pid)))) if has_mods(p)
                         else "bez modów")
        busy = state["preparing"] is not None or state["running"] is not None
        play_btn.config(state="disabled" if busy else "normal",
                        text="Uruchamianie…" if state["preparing"] else ("▶  W grze" if state["running"] else "▶  Graj"))
        stop_btn.config(state="normal" if state["running"] and state["running"]["pid"] == pid else "disabled")
        delete_btn.config(state="disabled" if pid == "default" or is_active(pid) else "normal")

    def refresh_account():
        if state["login"]:
            text = "Logowanie…"
        elif state["account"]:
            text = "●  " + state["account"]["name"]
        else:
            text = "Zaloguj się"
        account_btn.config(text=text + "  ▾")

    def refresh_all():
        draw_tiles()
        refresh_side()
        refresh_account()

    def select_profile(pid):
        if pid in pdata["profiles"] and pid != pdata["selected"]:
            pdata["selected"] = pid
            save_profiles(pdata)
            refresh_all()

    def context_menu(event):
        pid = pdata["selected"]
        menu = make_menu()
        menu.add_command(label="▶  Graj", command=lambda: play(pid))
        menu.add_command(label="Edytuj profil…", command=lambda: edit_profile(pid))
        menu.add_command(label="Mody…", command=lambda: edit_profile(pid, "mods"))
        menu.add_command(label="Pobierz mody i paczki…", command=lambda: open_downloader(pid))
        menu.add_command(label="Folder profilu", command=lambda: open_folder(profile_dir(pid)))
        menu.add_command(label="Kopiuj profil", command=copy_profile)
        menu.add_separator()
        menu.add_command(label="Usuń profil", command=delete_profile,
                         state="disabled" if pid == "default" or is_active(pid) else "normal")
        menu.tk_popup(event.x_root, event.y_root)

    def folders_menu():
        pid = pdata["selected"]
        menu = make_menu()
        menu.add_command(label="Folder launchera", command=lambda: open_folder(ROOT))
        menu.add_command(label="Folder profilu", command=lambda: open_folder(profile_dir(pid)))
        menu.add_command(label="Folder mods", command=lambda: open_folder(profile_dir(pid) / "mods"))
        return menu

    def account_menu():
        menu = make_menu()
        if state["account"]:
            menu.add_command(label=f"Zalogowano: {state['account']['name']}", state="disabled")
            menu.add_command(label="Wyloguj", command=logout)
        else:
            menu.add_command(label="Zaloguj przez Microsoft", command=login)
        return menu

    # ================================================================== logowanie
    def close_login_dialog():
        lg = state["login"]
        if lg and lg.get("dialog") and alive(lg["dialog"]):
            lg["dialog"].destroy()
        if lg:
            lg["dialog"] = None

    def cancel_login():
        lg = state["login"]
        if lg:
            lg["auth"].cancelled.set()
            close_login_dialog()
            set_status("Anulowanie logowania…")

    def open_login_dialog(code, uri):
        lg = state["login"]
        if not lg:
            return
        close_login_dialog()
        d = make_dialog("Logowanie Microsoft", 460, 310)
        lg["dialog"] = d
        make_label(d, "Zaloguj się kontem Microsoft", 18, bold=True, anchor="center").pack(fill="x", pady=(24, 6))
        make_label(d, f"Wejdź na {uri} i wpisz kod:", 13, MUTED, anchor="center").pack(fill="x")
        tk.Label(d, text=code, bg=BG, fg=GREEN, font=("Consolas", -38, "bold")).pack(pady=14)
        make_label(d, "Kod jest już w schowku. Okno zamknie się samo po zalogowaniu.", 12, MUTED,
                   anchor="center").pack(fill="x")
        row = tk.Frame(d, bg=BG)
        row.pack(pady=18)
        make_button(row, "Otwórz stronę", lambda: webbrowser.open(uri), "primary").pack(side="left", padx=4)
        make_button(row, "Kopiuj kod", lambda: copy(code)).pack(side="left", padx=4)
        make_button(row, "Anuluj", cancel_login).pack(side="left", padx=4)
        d.protocol("WM_DELETE_WINDOW", cancel_login)
        copy(code)

    def login():
        if state["login"]:
            dialog = state["login"].get("dialog")
            if dialog and alive(dialog):
                dialog.lift()
            return
        try:
            auth = MicrosoftAuth(cfg["msa_client_id"])
        except LauncherError as e:
            return set_status(str(e), True)
        state["login"] = {"auth": auth, "dialog": None}
        refresh_account()
        set_status("Logowanie…")

        def show_code(code, uri):
            ui(lambda: open_login_dialog(code, uri))
            webbrowser.open(uri)

        def done(account):
            close_login_dialog()
            state["login"], state["account"] = None, account
            refresh_account()
            set_status(f"Zalogowano jako {account['name']}")
            pid, state["play_after"] = state["play_after"], None
            if pid in pdata["profiles"]:
                play(pid)

        def fail(msg):
            close_login_dialog()
            state["login"], state["play_after"] = None, None
            refresh_account()
            set_status(msg, True)

        background(lambda: auth.login(show_code), done, fail)

    def logout():
        if state["login"]:
            return
        try:
            AUTH_FILE.unlink()
        except OSError:
            pass
        state["account"] = None
        refresh_account()
        set_status("Wylogowano")

    # ================================================================== gra
    def play(pid=None):
        pid = pid or pdata["selected"]
        if state["preparing"] or state["running"]:
            return set_status("Gra już działa albo właśnie się uruchamia.", True)
        if not state["account"]:
            state["play_after"] = pid  # po zalogowaniu gra wystartuje sama
            return login()
        game_dir, account = profile_dir(pid), state["account"]
        settings = launch_settings(cfg, profile(pid))
        state["preparing"] = pid
        set_progress(0)
        refresh_all()

        def log(msg):
            ui(lambda: set_status(msg))

        def progress(done_count, total):
            ui(lambda: set_progress(done_count / total if total else 0))

        def started(proc):
            state["preparing"], state["running"] = None, {"pid": pid, "proc": proc}
            set_progress(1)
            set_status(f"Gra uruchomiona ({profile(pid)['name'] if pid in pdata['profiles'] else pid})")
            refresh_all()

        def job():
            proc = prepare_and_launch(settings, account, log, progress, game_dir)
            ui(lambda: started(proc))
            for line in proc.stdout:
                ui(lambda line=line.rstrip(): write_log(line))
            return proc.wait()

        def done(code):
            state["preparing"], state["running"] = None, None
            set_progress(0)
            set_status(f"Gra zakończona (kod {code})")
            refresh_all()

        def fail(msg):
            state["preparing"], state["running"] = None, None
            set_progress(0)
            set_status(msg, True)
            refresh_all()

        background(job, done, fail)

    def stop_game():
        running = state["running"]
        if running:
            try:
                running["proc"].terminate()
                set_status("Zatrzymuję grę…")
            except OSError as e:
                set_status(f"Nie udało się zatrzymać gry: {e}", True)

    # ================================================================== profile
    def add_profile_dialog():
        d = make_dialog("Dodaj profil", 480, 300)
        f = tk.Frame(d, bg=BG)
        f.pack(fill="both", expand=True, padx=24, pady=18)
        f.columnconfigure(1, weight=1)
        make_label(f, "Nowy profil", 18, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        name_e, ver_cb = make_entry(f), make_combobox(f, state["versions"])
        loader_cb = make_combobox(f, loader_names, readonly=True)
        name_e.insert(0, f"Profil {len(pdata['profiles']) + 1}")
        ver_cb.set("latest")
        loader_cb.set(LOADER_LABELS["fabric"])
        form_row(f, 1, "Nazwa", name_e)
        form_row(f, 2, "Wersja gry", ver_cb)
        form_row(f, 3, "Loader modów", loader_cb)
        msg = make_label(f, "", 12, RED)
        msg.grid(row=4, column=0, columnspan=2, sticky="w")

        def create():
            name, version = name_e.get().strip()[:40], ver_cb.get().strip() or "latest"
            if not name:
                return msg.config(text="Podaj nazwę profilu.")
            if not valid_game_version(version):
                return msg.config(text="Wersja gry musi mieć postać np. 26.3 albo „latest”.")
            pid = new_profile_id(name, pdata["profiles"])
            pdata["profiles"][pid] = {"name": name, "game_version": version, "loader": loader_from_label(loader_cb.get())}
            pdata["selected"] = pid
            save_profiles(pdata)
            d.destroy()
            refresh_all()
            set_status(f"Utworzono profil „{name}”")

        row = tk.Frame(f, bg=BG)
        row.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
        make_button(row, "Anuluj", d.destroy).pack(side="right")
        make_button(row, "Utwórz", create, "primary").pack(side="right", padx=8)
        d.bind("<Return>", lambda e: create())
        name_e.focus_set()

    def copy_profile():
        pid, p = pdata["selected"], dict(profile())
        if is_active(pid):
            return set_status("Nie można kopiować profilu, gdy gra jest uruchomiona.", True)
        name = shorten(p["name"] + " (kopia)", 40)
        new = new_profile_id(name, pdata["profiles"])
        src, dst = profile_dir(pid), profile_dir(new)
        set_status(f"Kopiuję profil „{p['name']}”…")

        def done(_):
            pdata["profiles"][new] = dict(p, name=name)
            pdata["selected"] = new
            save_profiles(pdata)
            refresh_all()
            set_status(f"Skopiowano profil jako „{name}”")

        background(lambda: copy_profile_files(src, dst), done, lambda m: set_status(m, True))

    def delete_profile():
        pid = pdata["selected"]
        if pid == "default":
            return set_status("Profilu domyślnego nie można usunąć.", True)
        if is_active(pid):
            return set_status("Zamknij grę przed usunięciem profilu.", True)
        folder = profile_dir(pid)
        if not messagebox.askyesno(NAME, f"Usunąć profil „{profile()['name']}” z listy?\n\n"
                                         f"Pliki (światy, mody) zostaną na dysku w:\n{folder}", parent=root):
            return
        for store in ("editors", "downloaders"):
            window = state[store].pop(pid, None)
            if window and alive(window["win"]):
                window["win"].destroy()
        del pdata["profiles"][pid]
        pdata["selected"] = "default" if "default" in pdata["profiles"] else next(iter(pdata["profiles"]))
        save_profiles(pdata)
        refresh_all()
        set_status(f"Usunięto profil z listy. Jego pliki zostały w {folder}")

    def mods_changed(pid):
        editor = state["editors"].get(pid)
        if editor and alive(editor["win"]):
            editor["refresh_mods"]()
        downloader = state["downloaders"].get(pid)
        if downloader and alive(downloader["win"]):
            downloader["render"]()
        if pid == pdata["selected"]:
            refresh_side()

    # ============================================================= edycja profilu
    def edit_profile(pid=None, page="version"):
        pid = pid or pdata["selected"]
        editor = state["editors"].get(pid)
        if editor and alive(editor["win"]):
            editor["show"](page)
            editor["win"].lift()
            return
        p = pdata["profiles"][pid]
        d = make_dialog(f"Profil – {p['name']}", 800, 540, resizable=True)
        nav = tk.Frame(d, bg=PANEL, width=180)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        content = tk.Frame(d, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        pages, nav_buttons = {}, {}
        editor = {"win": d}
        state["editors"][pid] = editor

        # --- Wersja
        pv = tk.Frame(content, bg=BG)
        pv.columnconfigure(1, weight=1)
        make_label(pv, "Wersja", 18, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        name_e, ver_cb = make_entry(pv), make_combobox(pv, state["versions"])
        loader_cb, lv_e = make_combobox(pv, loader_names, readonly=True), make_entry(pv)
        name_e.insert(0, p["name"])
        ver_cb.set(p["game_version"])
        loader_cb.set(LOADER_LABELS[p.get("loader", "fabric")])
        lv_e.insert(0, p.get("loader_version", ""))
        form_row(pv, 1, "Nazwa", name_e)
        form_row(pv, 2, "Wersja gry („latest” = najnowsza)", ver_cb)
        form_row(pv, 3, "Loader modów", loader_cb)
        form_row(pv, 4, "Wersja loadera (puste = najnowsza)", lv_e)
        pv_msg = make_label(pv, "", 12, GREEN, wraplength=520)
        pv_msg.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        make_label(pv, "Forge i NeoForge instalują się przy pierwszym uruchomieniu (kilka minut).", 12, MUTED,
                   wraplength=520).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def save_version():
            name, version = name_e.get().strip()[:40], ver_cb.get().strip() or "latest"
            if is_active(pid):
                return pv_msg.config(text="Zamknij grę przed zmianą wersji.", fg=RED)
            if not name:
                return pv_msg.config(text="Nazwa profilu nie może być pusta.", fg=RED)
            if not valid_game_version(version):
                return pv_msg.config(text="Wersja gry musi mieć postać np. 26.3 albo „latest”.", fg=RED)
            loader, pin = loader_from_label(loader_cb.get()), lv_e.get().strip()[:60]
            if pin and not re.fullmatch(r"[0-9A-Za-z.+_-]+", pin):
                return pv_msg.config(text="Wersja loadera może zawierać tylko litery, cyfry i znaki . + _ -", fg=RED)
            old_loader = p.get("loader", "fabric")
            if loader != old_loader:
                pin = "" if pin == p.get("loader_version", "") else pin  # stara wersja nie pasuje do nowego loadera
            p.update(name=name, game_version=version, loader=loader)
            if pin and loader != "vanilla":
                p["loader_version"] = pin
            else:
                p.pop("loader_version", None)
            lv_e.delete(0, "end")
            lv_e.insert(0, p.get("loader_version", ""))
            save_profiles(pdata)
            d.title(f"Profil – {name}")
            refresh_all()
            refresh_lists()
            pv_msg.config(text="Zapisano.", fg=GREEN)

        make_button(pv, "Zapisz", save_version, "primary").grid(row=5, column=1, sticky="w", pady=(12, 0))
        pages["version"] = pv

        # --- Mody
        pm = tk.Frame(content, bg=BG)
        make_label(pm, "Mody", 18, bold=True).pack(fill="x", pady=(0, 10))
        mods_bar = tk.Frame(pm, bg=BG)
        mods_bar.pack(fill="x")
        mods_lb = make_listbox(pm, 14)
        mods_lb.pack(fill="both", expand=True, pady=(10, 6))
        pm_msg = make_label(pm, "", 12, MUTED, wraplength=560)
        pm_msg.pack(fill="x")
        rows = []

        def refresh_mods():
            rows[:] = list_installed(profile_dir(pid))
            mods_lb.delete(0, "end")
            for m in rows:
                label = m["title"] if m["title"] == m["base"] else f"{m['title']}   ·   {m['base']}"
                mods_lb.insert("end", ("☑  " if m["enabled"] else "☐  ") + label)
            if not has_mods(p):
                pm_msg.config(text="Ten profil nie ma loadera modów, więc mody nie zostaną wczytane.", fg=RED)
            elif not rows:
                pm_msg.config(text="Brak modów. Kliknij „Pobierz mody…”.", fg=MUTED)
            else:
                pm_msg.config(text=f"{plural_mods(len(rows))}, włączone: {sum(m['enabled'] for m in rows)}. "
                                   "Dwuklik włącza lub wyłącza mod.", fg=MUTED)

        def selected_mod():
            sel = mods_lb.curselection()
            return rows[sel[0]] if sel and sel[0] < len(rows) else None

        def toggle(_event=None):
            m = selected_mod()
            if not m:
                return pm_msg.config(text="Zaznacz mod na liście.", fg=RED)
            try:
                enabled = toggle_mod(profile_dir(pid), m["file"])
            except (OSError, LauncherError) as e:
                return pm_msg.config(text=f"Nie udało się: {e}", fg=RED)
            mods_changed(pid)
            pm_msg.config(text=f"{'Włączono' if enabled else 'Wyłączono'}: {m['title']}", fg=GREEN)

        def remove():
            m = selected_mod()
            if not m:
                return pm_msg.config(text="Zaznacz mod na liście.", fg=RED)
            if not messagebox.askyesno(NAME, f"Usunąć plik {m['base']} z profilu?", parent=d):
                return
            try:
                remove_mod(profile_dir(pid), m["file"])
            except (OSError, LauncherError) as e:
                return pm_msg.config(text=f"Nie udało się usunąć: {e}", fg=RED)
            mods_changed(pid)
            pm_msg.config(text=f"Usunięto: {m['title']}", fg=GREEN)

        make_button(mods_bar, "Pobierz mody…", lambda: open_downloader(pid), "primary").pack(side="left")
        make_button(mods_bar, "Włącz / wyłącz", toggle).pack(side="left", padx=(8, 0))
        make_button(mods_bar, "Usuń", remove, "danger").pack(side="left", padx=(8, 0))
        make_button(mods_bar, "Folder mods", lambda: open_folder(profile_dir(pid) / "mods")).pack(side="left", padx=(8, 0))
        make_button(mods_bar, "Odśwież", lambda: mods_changed(pid)).pack(side="left", padx=(8, 0))
        mods_lb.bind("<Double-Button-1>", toggle)
        pages["mods"] = pm

        # --- Ustawienia
        ps = tk.Frame(content, bg=BG)
        ps.columnconfigure(1, weight=1)
        make_label(ps, "Ustawienia profilu", 18, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ram_e, jvm_e = make_entry(ps), make_entry(ps)
        ram_e.insert(0, str(p.get("memory_mb") or ""))
        jvm_e.insert(0, p.get("jvm_args", ""))
        form_row(ps, 1, "Pamięć RAM (MB)", ram_e)
        make_label(ps, f"Puste = ustawienie globalne ({cfg.get('memory_mb', 4096)} MB)", 12, MUTED).grid(
            row=2, column=1, sticky="w")
        form_row(ps, 3, "Dodatkowe argumenty JVM", jvm_e)
        ps_msg = make_label(ps, "", 12, GREEN)
        ps_msg.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def save_settings():
            text = ram_e.get().strip()
            if text:
                try:
                    ram = int(text)
                except ValueError:
                    return ps_msg.config(text="Pamięć RAM musi być liczbą, np. 4096.", fg=RED)
                if not 1024 <= ram <= 65536:
                    return ps_msg.config(text="Pamięć RAM musi być w zakresie 1024–65536 MB.", fg=RED)
                p["memory_mb"] = ram
            else:
                p.pop("memory_mb", None)
            args = jvm_e.get().strip()
            if args:
                p["jvm_args"] = args[:500]
            else:
                p.pop("jvm_args", None)
            save_profiles(pdata)
            ps_msg.config(text="Zapisano. Zmiany zadziałają przy następnym uruchomieniu gry.", fg=GREEN)

        make_button(ps, "Zapisz", save_settings, "primary").grid(row=4, column=1, sticky="w", pady=(12, 0))
        pages["settings"] = ps

        # --- Paczki zasobów
        pr = tk.Frame(content, bg=BG)
        make_label(pr, "Paczki zasobów", 18, bold=True).pack(fill="x", pady=(0, 10))
        rp_bar = tk.Frame(pr, bg=BG)
        rp_bar.pack(fill="x")
        rp_lb = make_listbox(pr, 14)
        rp_lb.pack(fill="both", expand=True, pady=(10, 6))
        rp_msg = make_label(pr, "", 12, MUTED, wraplength=560)
        rp_msg.pack(fill="x")
        rp_rows = []

        def refresh_packs():
            game_dir = profile_dir(pid)
            rp_rows[:] = list_packs(game_dir / "resourcepacks", resourcepack_index(game_dir))
            rp_lb.delete(0, "end")
            for m in rp_rows:
                label = m["title"] if m["title"] == m["file"] else f"{m['title']}   ·   {m['file']}"
                rp_lb.insert("end", label + ("   (folder)" if m["folder"] else ""))
            rp_msg.config(text="Włączysz je w grze: Opcje → Paczki zasobów." if rp_rows
                          else "Brak paczek zasobów. Kliknij „Pobierz paczki zasobów…”.", fg=MUTED)

        def remove_rp():
            sel = rp_lb.curselection()
            m = rp_rows[sel[0]] if sel and sel[0] < len(rp_rows) else None
            if not m:
                return rp_msg.config(text="Zaznacz paczkę na liście.", fg=RED)
            if not messagebox.askyesno(NAME, f"Usunąć {m['file']} z profilu?", parent=d):
                return
            game_dir = profile_dir(pid)
            try:
                remove_pack(game_dir / "resourcepacks", resourcepack_index(game_dir), m["file"])
            except (OSError, LauncherError) as e:
                return rp_msg.config(text=f"Nie udało się usunąć: {e}", fg=RED)
            mods_changed(pid)
            rp_msg.config(text=f"Usunięto: {m['title']}", fg=GREEN)

        make_button(rp_bar, "Pobierz paczki zasobów…", lambda: open_downloader(pid, "resourcepack"),
                    "primary").pack(side="left")
        make_button(rp_bar, "Usuń", remove_rp, "danger").pack(side="left", padx=(8, 0))
        make_button(rp_bar, "Folder", lambda: open_folder(profile_dir(pid) / "resourcepacks")).pack(side="left", padx=(8, 0))
        make_button(rp_bar, "Odśwież", lambda: mods_changed(pid)).pack(side="left", padx=(8, 0))
        pages["resourcepacks"] = pr

        # --- Światy i paczki danych
        pw = tk.Frame(content, bg=BG)
        make_label(pw, "Światy i paczki danych", 18, bold=True).pack(fill="x", pady=(0, 10))
        w_bar = tk.Frame(pw, bg=BG)
        w_bar.pack(fill="x")
        w_lb = make_listbox(pw, 14)
        w_lb.pack(fill="both", expand=True, pady=(10, 6))
        w_msg = make_label(pw, "", 12, MUTED, wraplength=560)
        w_msg.pack(fill="x")
        w_rows = []

        def refresh_worlds():
            w_rows[:] = list_worlds(profile_dir(pid))
            w_lb.delete(0, "end")
            for world in w_rows:
                count = len(list_packs(world / "datapacks", world / "zorza_datapacks.json"))
                w_lb.insert("end", f"{world.name}   ·   paczki danych: {count}")
            w_msg.config(text="Paczki danych instaluje się do konkretnego świata." if w_rows
                         else "Brak światów. Uruchom grę i utwórz świat.", fg=MUTED)

        def selected_world():
            sel = w_lb.curselection()
            if not sel or sel[0] >= len(w_rows):
                w_msg.config(text="Zaznacz świat na liście.", fg=RED)
                return None
            return w_rows[sel[0]]

        def open_world(sub=""):
            world = selected_world()
            if world:
                open_folder(world / sub if sub else world)

        make_button(w_bar, "Pobierz paczki danych…", lambda: open_downloader(pid, "datapack"),
                    "primary").pack(side="left")
        make_button(w_bar, "Folder datapacks", lambda: open_world("datapacks")).pack(side="left", padx=(8, 0))
        make_button(w_bar, "Folder świata", lambda: open_world()).pack(side="left", padx=(8, 0))
        make_button(w_bar, "Odśwież", lambda: refresh_worlds()).pack(side="left", padx=(8, 0))
        pages["worlds"] = pw

        def refresh_lists():
            refresh_mods()
            refresh_packs()
            refresh_worlds()

        editor["refresh_mods"] = refresh_lists

        def show(page_name):
            for frame in pages.values():
                frame.pack_forget()
            pages[page_name].pack(fill="both", expand=True, padx=22, pady=18)
            for key, button in nav_buttons.items():
                button.config(bg=SELECT if key == page_name else PANEL)
            if page_name in ("mods", "resourcepacks", "worlds"):
                refresh_lists()

        make_label(nav, shorten(p["name"], 18), 14, TEXT, bold=True, bg=PANEL).pack(fill="x", padx=16, pady=(18, 10))
        for key, label in (("version", "Wersja"), ("mods", "Mody"), ("resourcepacks", "Paczki zasobów"),
                           ("worlds", "Światy"), ("settings", "Ustawienia")):
            button = tk.Button(nav, text=label, anchor="w", relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
                               font=font(13), bg=PANEL, fg=TEXT, activebackground=SELECT, activeforeground=TEXT,
                               highlightthickness=0, command=lambda k=key: show(k))
            button.pack(fill="x")
            nav_buttons[key] = button
        editor["show"] = show

        def close():
            state["editors"].pop(pid, None)
            d.destroy()

        d.protocol("WM_DELETE_WINDOW", close)
        show(page)

    # ============================================================ pobieranie treści
    def pick_world(pid, parent, on_pick):
        worlds = list_worlds(profile_dir(pid))
        if not worlds:
            messagebox.showinfo(NAME, "Ten profil nie ma jeszcze żadnego świata.\n\n"
                                      "Uruchom grę, utwórz świat i spróbuj ponownie.", parent=parent)
            return
        d = make_dialog("Wybierz świat", 460, 400)
        make_label(d, "Do którego świata dodać paczkę danych?", 15, bold=True).pack(fill="x", padx=20, pady=(18, 8))
        row = tk.Frame(d, bg=BG)
        row.pack(side="bottom", fill="x", padx=20, pady=14)
        listbox = make_listbox(d, 10)
        listbox.pack(fill="both", expand=True, padx=20)
        for world in worlds:
            listbox.insert("end", world.name)

        def choose(_event=None):
            sel = listbox.curselection()
            world = worlds[sel[0] if sel and sel[0] < len(worlds) else 0]
            d.destroy()
            on_pick(world)

        make_button(row, "Anuluj", d.destroy).pack(side="right")
        make_button(row, "Dodaj do świata", choose, "primary").pack(side="right", padx=8)
        listbox.bind("<Double-Button-1>", choose)

    def start_modpack_install(job, fallback_name, pack_meta=None, finished=None):
        """job(folder_profilu, postep) -> info. Po udanej instalacji tworzy nowy profil."""
        if state["modpack_busy"]:
            set_status("Inny modpack właśnie się instaluje.", True)
            return False
        pid = new_profile_id(fallback_name, pdata["profiles"])
        game_dir = profile_dir(pid)
        state["modpack_busy"] = True
        set_progress(0)
        set_status(f"Instaluję modpack „{fallback_name}”…")

        def progress(done_count, total):
            ui(lambda: set_progress(done_count / total if total else 0))

        def done(info):
            state["modpack_busy"] = False
            set_progress(0)
            name = shorten(info.get("name") or fallback_name, 40)
            p = {"name": name, "game_version": info["game_version"], "loader": info.get("loader", "fabric")}
            if info.get("loader_version"):
                p["loader_version"] = info["loader_version"]
            if pack_meta:
                p["pack"] = dict(pack_meta, version=info.get("version", ""))
            pdata["profiles"][pid] = p
            pdata["selected"] = pid
            save_profiles(pdata)
            refresh_all()
            msg = f"Zainstalowano modpack „{name}” jako nowy profil ({info.get('files', 0)} plików)"
            set_status(msg)
            notes = []
            if info.get("missing"):
                notes.append(f"{info['missing']} plików z tego modpacka nie ma już na CurseForge.")
            if info.get("blocked"):
                lines = "\n".join(f"• {b['name']} ({b['file']}) → folder {b['folder']}" for b in info["blocked"][:15])
                notes.append("Autorzy tych plików nie pozwalają pobierać ich przez launchery. Pobierz je ze strony "
                             "CurseForge i wrzuć do podanego folderu profilu (linki są w Konsoli):\n" + lines)
                for b in info["blocked"]:
                    write_log(f"Do pobrania ręcznie: {b['name']} ({b['file']}) {b.get('url') or ''}")
            if notes:
                messagebox.showwarning(NAME, msg + "\n\n" + "\n\n".join(notes), parent=root)
            if finished:
                finished(name)

        def fail(msg):
            state["modpack_busy"] = False
            set_progress(0)
            shutil.rmtree(game_dir, ignore_errors=True)  # folder utworzony wyłącznie przez tę instalację
            set_status(msg, True)
            if finished:
                finished(None, msg)

        background(lambda: job(game_dir, progress), done, fail)
        return True

    def import_modpack():
        path = filedialog.askopenfilename(parent=root, title="Wybierz plik modpacka",
                                          filetypes=[("Modpack", "*.mrpack *.zip"), ("Wszystkie pliki", "*.*")])
        if not path:
            return
        key = curseforge_key(cfg)
        start_modpack_install(lambda game_dir, prog: install_modpack_file(Path(path), game_dir, key, prog),
                              Path(path).stem)

    def open_downloader(pid=None, kind="mod"):
        pid = pid or pdata["selected"]
        dl = state["downloaders"].get(pid)
        if dl and alive(dl["win"]):
            dl["set_kind"](kind)
            dl["win"].lift()
            return
        d = make_dialog(f"Pobierz – {profile(pid)['name']}", 1040, 660, resizable=True)
        dl = {"win": d, "kind": kind, "source": "modrinth", "results": [], "key": None, "req": 0, "end": False,
              "details": {}, "icons": {}, "detail_req": 0, "links": []}
        state["downloaders"][pid] = dl

        tabs = tk.Frame(d, bg=PANEL)
        tabs.pack(fill="x")
        kind_buttons, source_buttons = {}, {}
        for key, t in CONTENT_TYPES.items():
            button = make_button(tabs, t["label"], lambda k=key: set_kind(k))
            button.pack(side="left", padx=(12 if not kind_buttons else 2, 0), pady=(10, 0))
            kind_buttons[key] = button
        for key, label in (("curseforge", "CurseForge"), ("modrinth", "Modrinth")):
            button = make_button(tabs, label, lambda k=key: set_source(k))
            button.pack(side="right", padx=(2, 12 if key == "curseforge" else 0), pady=(10, 0))
            source_buttons[key] = button

        top = tk.Frame(d, bg=PANEL)
        top.pack(fill="x")
        query = make_entry(top)
        query.pack(side="left", fill="x", expand=True, padx=(12, 8), pady=10, ipady=4)
        sort_cb = make_combobox(top, [s[0] for s in SORTS], readonly=True)
        sort_cb.set(SORTS[0][0])
        sort_cb.pack(side="left")
        make_button(top, "Szukaj", lambda: load(force=True), "primary").pack(side="left", padx=12)

        bottom = tk.Frame(d, bg=PANEL)
        bottom.pack(side="bottom", fill="x")
        make_button(bottom, "Pokaż więcej", lambda: load(more=True)).pack(side="left", padx=12, pady=8)
        info = make_label(bottom, "", 12, MUTED, bg=PANEL)
        info.pack(side="left", padx=6)
        make_button(bottom, "Zamknij", lambda: close()).pack(side="right", padx=12)
        target = make_label(bottom, "", 12, MUTED, bg=PANEL)
        target.pack(side="right", padx=6)

        content = tk.Frame(d, bg=BG)
        content.pack(fill="both", expand=True)
        details = tk.Frame(content, bg=PANEL, width=380)
        details.pack(side="right", fill="y")
        details.pack_propagate(False)
        results_lb = make_listbox(content, 20)
        results_scroll = ttk.Scrollbar(content, orient="vertical", command=results_lb.yview)
        results_lb.configure(yscrollcommand=results_scroll.set)
        results_scroll.pack(side="right", fill="y", pady=12)
        results_lb.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)

        head = tk.Frame(details, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(16, 6))
        icon_lbl = tk.Label(head, bg=PANEL)
        icon_lbl.pack(side="left", padx=(0, 12))
        head_text = tk.Frame(head, bg=PANEL)
        head_text.pack(side="left", fill="x", expand=True)
        det_title = make_label(head_text, "", 17, TEXT, bold=True, bg=PANEL, wraplength=250)
        det_title.pack(fill="x")
        det_author = make_label(head_text, "", 12, MUTED, bg=PANEL, wraplength=250)
        det_author.pack(fill="x")
        det_meta = make_label(details, "", 12, MUTED, bg=PANEL, wraplength=340)
        det_meta.pack(fill="x", padx=16)
        links_frame = tk.Frame(details, bg=PANEL)
        links_frame.pack(fill="x", padx=14, pady=(8, 2))
        install_btn = make_button(details, "Zainstaluj", lambda: install(), "primary")
        install_btn.pack(side="bottom", fill="x", padx=16, pady=(6, 16), ipady=3)
        det_msg = make_label(details, "", 12, MUTED, bg=PANEL, wraplength=340)
        det_msg.pack(side="bottom", fill="x", padx=16)
        desc = tk.Text(details, bg=PANEL, fg=TEXT, relief="flat", bd=0, wrap="word", font=font(12), padx=0, pady=4,
                       highlightthickness=0, cursor="arrow", height=10)
        desc_scroll = ttk.Scrollbar(details, orient="vertical", command=desc.yview)
        desc.configure(yscrollcommand=desc_scroll.set)
        desc_scroll.pack(side="right", fill="y", pady=8)
        desc.pack(fill="both", expand=True, padx=(16, 0), pady=8)

        def set_desc(text):
            desc.config(state="normal")
            desc.delete("1.0", "end")
            desc.insert("1.0", text)
            desc.config(state="disabled")

        def set_links(links):
            for button in dl["links"]:
                button.destroy()
            dl["links"] = []
            for i, (label, url) in enumerate(links[:6]):
                button = make_button(links_frame, label, lambda u=url: webbrowser.open(u))
                button.grid(row=i // 2, column=i % 2, sticky="we", padx=2, pady=2)
                dl["links"].append(button)
            links_frame.columnconfigure(0, weight=1)
            links_frame.columnconfigure(1, weight=1)

        def installed_keys():
            kind, game_dir = dl["kind"], profile_dir(pid)
            if kind == "modpack":
                return {(p["pack"].get("source"), str(p["pack"].get("id")))
                        for p in pdata["profiles"].values() if p.get("pack")}
            if kind == "mod":
                index = load_mods_index(game_dir)
            elif kind == "resourcepack":
                index = _load_index(resourcepack_index(game_dir))
            else:
                return set()  # paczki danych należą do konkretnych światów
            return {(i.get("source"), str(i.get("id"))) for i in index.values()}

        def selected():
            sel = results_lb.curselection()
            return dl["results"][sel[0]] if sel and sel[0] < len(dl["results"]) else None

        def update_install_button(r):
            kind = dl["kind"]
            text = {"mod": "Zainstaluj", "resourcepack": "Zainstaluj", "datapack": "Dodaj do świata…",
                    "modpack": "Zainstaluj jako nowy profil"}[kind]
            if kind in ("mod", "resourcepack") and (r["source"], str(r["id"])) in installed_keys():
                text = "Zainstaluj ponownie / zaktualizuj"
            install_btn.config(text=text, state="normal")

        def show_icon(r, req):
            icon_lbl.config(image="")
            url = r.get("icon") or ""
            if not url:
                return
            if url in dl["icons"]:
                if dl["icons"][url]:
                    icon_lbl.config(image=dl["icons"][url])
                return

            def done(data):
                img = None
                if data:
                    try:
                        img = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
                        factor = max(1, img.width() // 64)
                        if factor > 1:
                            img = img.subsample(factor)
                    except Exception:  # nieobslugiwany format albo uszkodzony plik
                        img = None
                dl["icons"][url] = img  # referencja, zeby obraz nie zniknal
                if img and alive(d) and req == dl["detail_req"]:
                    icon_lbl.config(image=img)

            background(lambda: fetch_icon(url), done, lambda msg: dl["icons"].__setitem__(url, None))

        def apply_details(r, extra):
            meta = [f"{format_count(r['downloads'])} pobrań"]
            if extra.get("followers"):
                meta.append(f"{format_count(int(extra['followers']))} obserwujących")
            if extra.get("updated"):
                meta.append(f"aktualizacja: {extra['updated']}")
            if extra.get("license"):
                meta.append(f"licencja: {extra['license']}")
            text = "  ·  ".join(meta)
            if extra.get("categories"):
                text += "\nKategorie: " + ", ".join(extra["categories"][:8])
            det_meta.config(text=text)
            set_desc(extra.get("description") or r.get("description") or "Brak opisu.")
            links = extra.get("links") or ([("Strona projektu", r["url"])] if str(r.get("url", "")).startswith("https://") else [])
            set_links(links)

        def show_details(_event=None):
            r = selected()
            dl["detail_req"] += 1
            req = dl["detail_req"]
            if not r:
                for widget in (det_title, det_author, det_meta):
                    widget.config(text="")
                icon_lbl.config(image="")
                set_desc("Wybierz pozycję z listy, żeby zobaczyć opis.")
                set_links([])
                install_btn.config(state="disabled")
                return
            det_title.config(text=r["title"])
            det_author.config(text=f"autor: {r['author']}" if r.get("author") else "")
            det_meta.config(text=f"{format_count(r['downloads'])} pobrań")
            set_desc((r.get("description") or "") + "\n\nWczytuję pełny opis…")
            set_links([("Strona projektu", r["url"])] if str(r.get("url", "")).startswith("https://") else [])
            update_install_button(r)
            show_icon(r, req)
            cache_key = (r["source"], str(r["id"]))
            if cache_key in dl["details"]:
                return apply_details(r, dl["details"][cache_key])
            key = curseforge_key(cfg)

            def done(extra):
                dl["details"][cache_key] = extra
                if alive(d) and req == dl["detail_req"]:
                    apply_details(r, extra)

            def fail(msg):
                write_log(f"Nie udało się pobrać opisu „{r['title']}”: {msg}")
                if alive(d) and req == dl["detail_req"]:
                    set_desc(r.get("description") or "Nie udało się wczytać opisu.")

            background(lambda: project_details(r, key), done, fail)

        def render():
            have = installed_keys()
            results_lb.delete(0, "end")
            for r in dl["results"]:
                mark = "✓  " if (r["source"], str(r["id"])) in have else "     "
                results_lb.insert("end", f"{mark}{r['title']}   ·   {format_count(r['downloads'])} pobrań")
            show_details()

        dl["render"] = render

        def ready():
            p = pdata["profiles"].get(pid)
            problem = None
            if not p:
                problem = "Ten profil został usunięty."
            elif dl["kind"] == "mod" and not has_mods(p):
                problem = "Ten profil nie ma loadera modów. Wybierz Fabric, Quilt, Forge albo NeoForge w edycji profilu."
            elif dl["source"] == "curseforge" and not curseforge_key(cfg):
                problem = "Ta wersja nie ma wbudowanego klucza CurseForge. Wpisz własny w Ustawieniach."
            if problem:
                dl["results"], dl["key"] = [], None
                render()
                info.config(text=problem, fg=RED)
                return False
            return True

        def load(more=False, force=False):
            """Bez wpisanego tekstu pokazuje najpopularniejsze pozycje; more=True doczytuje kolejną stronę."""
            if not ready():
                return
            kind, src, text, key = dl["kind"], dl["source"], query.get().strip(), curseforge_key(cfg)
            sort = dict(SORTS).get(sort_cb.get(), "downloads") if src == "modrinth" else None
            if src == "modrinth" and not text and sort == "relevance":
                sort = "downloads"  # bez zapytania "trafność" nie ma sensu
            wanted, loader = profile(pid)["game_version"], profile(pid).get("loader", "fabric")
            request_key = (kind, src, wanted, loader, text, sort)
            if more and (dl["key"] != request_key or dl["end"]):
                return
            if not more and not force and request_key == dl["key"] and dl["results"]:
                return render()
            offset = len(dl["results"]) if more else 0
            dl["req"] += 1
            req = dl["req"]
            info.config(text="Wczytuję kolejne…" if more else ("Szukam…" if text else "Wczytuję najpopularniejsze…"),
                        fg=MUTED)

            def job():
                version = resolve_game_version(wanted)
                return version, search_content(kind, src, text, version, key, PAGE, offset, sort, loader)

            def done(result):
                if req != dl["req"] or not alive(d):
                    return  # w międzyczasie zlecono nowsze wyszukiwanie albo okno zamknięto
                version, hits = result
                if more:
                    seen = {(h["source"], str(h["id"])) for h in dl["results"]}
                    hits = [h for h in hits if (h["source"], str(h["id"])) not in seen]
                    dl["results"] += hits
                else:
                    dl["results"] = hits
                dl["key"], dl["end"] = request_key, len(hits) < PAGE
                render()
                where = "Modrinth" if src == "modrinth" else "CurseForge"
                label = CONTENT_TYPES[kind]["label"].lower()
                for_version = "" if kind == "modpack" else f" dla Minecraft {version}"
                if not dl["results"]:
                    info.config(text=f"{where}: brak wyników{for_version}", fg=MUTED)
                elif text:
                    info.config(text=f"{where}: {len(dl['results'])} wyników dla „{shorten(text, 30)}”{for_version}",
                                fg=MUTED)
                else:
                    info.config(text=f"{where}: najpopularniejsze {label}{for_version} ({len(dl['results'])})",
                                fg=MUTED)

            def fail(msg):
                if req == dl["req"] and alive(d):
                    info.config(text=shorten(msg, 110), fg=RED)
                write_log("BŁĄD: " + msg)

            background(job, done, fail)

        def update_target():
            name = profile(pid)["name"] if pid in pdata["profiles"] else "?"
            target.config(text={"mod": f"Instalacja do profilu „{shorten(name, 24)}”",
                                "resourcepack": f"Instalacja do profilu „{shorten(name, 24)}”",
                                "datapack": "Paczka danych trafi do wybranego świata",
                                "modpack": "Modpack zostanie zainstalowany jako nowy profil"}[dl["kind"]])

        def set_kind(kind):
            dl["kind"] = kind
            for key, button in kind_buttons.items():
                button.config(bg=SELECT if key == kind else PANEL2)
            d.title(f"{CONTENT_TYPES[kind]['label']} – Zorza Client")
            update_target()
            load()

        def set_source(src):
            dl["source"] = src
            for key, button in source_buttons.items():
                button.config(bg=SELECT if key == src else PANEL2)
            sort_cb.config(state="readonly" if src == "modrinth" else "disabled")
            load()

        dl["set_kind"] = set_kind

        def install():
            r = selected()
            if not r:
                return det_msg.config(text="Zaznacz pozycję na liście.", fg=RED)
            if not ready():
                return
            kind, key = dl["kind"], curseforge_key(cfg)
            if kind == "modpack":
                def finished(name, error=None):
                    if alive(d):
                        det_msg.config(text=error or f"Gotowe: utworzono profil „{name}”.", fg=RED if error else GREEN)
                        if not error:
                            render()

                if start_modpack_install(lambda game_dir, prog: install_modpack(r, game_dir, key, prog), r["title"],
                                         {"source": r["source"], "id": str(r["id"])}, finished):
                    det_msg.config(text=f"Instaluję modpack „{r['title']}”… Postęp widać na dole głównego okna.",
                                   fg=MUTED)
                else:
                    det_msg.config(text="Inny modpack właśnie się instaluje.", fg=RED)
                return
            if is_active(pid):
                return det_msg.config(text="Zamknij grę przed instalowaniem.", fg=RED)
            wanted, game_dir, loader = profile(pid)["game_version"], profile_dir(pid), profile(pid).get("loader", "fabric")

            def run(target_dir, index_path, where=""):
                install_btn.config(state="disabled")
                det_msg.config(text=f"Instaluję: {r['title']}…", fg=MUTED)

                def job():
                    names = []
                    version = resolve_game_version(wanted)
                    try:
                        if kind == "mod":
                            install_mod(r, version, game_dir, key, names.append, loader)
                        else:
                            install_pack(r, kind, version, target_dir, index_path, key, names.append)
                    except LauncherError as e:
                        raise LauncherError(str(e) + (f" (pobrano już: {', '.join(names)})" if names else ""))
                    return names

                def done(names):
                    mods_changed(pid)
                    if alive(d):
                        det_msg.config(text="Zainstalowano: " + ", ".join(names) + where, fg=GREEN)
                    set_status(f"Zainstalowano w profilu „{profile(pid)['name']}”: {', '.join(names)}{where}")

                def fail(msg):
                    mods_changed(pid)  # część zależności mogła się już pobrać
                    if alive(d):
                        det_msg.config(text=msg, fg=RED)
                    write_log("BŁĄD: " + msg)

                background(job, done, fail)

            if kind == "mod":
                run(game_dir / "mods", None)
            elif kind == "resourcepack":
                run(game_dir / "resourcepacks", resourcepack_index(game_dir))
            else:
                pick_world(pid, d, lambda world: run(world / "datapacks", world / "zorza_datapacks.json",
                                                     f" (świat: {world.name})"))

        def close():
            state["downloaders"].pop(pid, None)
            d.destroy()

        results_lb.bind("<<ListboxSelect>>", show_details)
        results_lb.bind("<Double-Button-1>", lambda e: install())
        query.bind("<Return>", lambda e: load(force=True))
        sort_cb.bind("<<ComboboxSelected>>", lambda e: load(force=True))
        d.protocol("WM_DELETE_WINDOW", close)
        dl["source"] = "modrinth"
        for key, button in source_buttons.items():
            button.config(bg=SELECT if key == "modrinth" else PANEL2)
        set_kind(kind)  # od razu wczytuje najpopularniejsze pozycje
        query.focus_set()

    # ============================================================ ustawienia, konsola
    def settings_dialog():
        d = make_dialog("Ustawienia", 600, 340)
        f = tk.Frame(d, bg=BG)
        f.pack(fill="both", expand=True, padx=24, pady=18)
        f.columnconfigure(1, weight=1)
        make_label(f, "Ustawienia launchera", 18, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ram_e, java_e, cf_e = make_entry(f), make_entry(f), make_entry(f, show="•")
        ram_e.insert(0, str(cfg.get("memory_mb", 4096)))
        java_e.insert(0, cfg.get("java_path", ""))
        cf_e.insert(0, cfg.get("curseforge_api_key", ""))
        form_row(f, 1, "Pamięć RAM dla gry (MB)", ram_e)
        form_row(f, 2, "Ścieżka do Javy (puste = automatycznie)", java_e)
        form_row(f, 3, "Własny klucz API CurseForge", cf_e)
        make_label(f, "Puste = klucz wbudowany w launcher" if BUILTIN_CURSEFORGE_KEY
                   else "Ta wersja nie ma wbudowanego klucza", 12, MUTED).grid(row=6, column=1, sticky="w")
        msg = make_label(f, "", 12, GREEN)
        msg.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def save():
            try:
                ram = int(ram_e.get().strip())
            except ValueError:
                return msg.config(text="Pamięć RAM musi być liczbą, np. 4096.", fg=RED)
            if not 1024 <= ram <= 65536:
                return msg.config(text="Pamięć RAM musi być w zakresie 1024–65536 MB.", fg=RED)
            cfg.update(memory_mb=ram, java_path=java_e.get().strip(), curseforge_api_key=cf_e.get().strip())
            save_config(cfg)
            msg.config(text="Zapisano.", fg=GREEN)
            set_status("Zapisano ustawienia launchera")

        row = tk.Frame(f, bg=BG)
        row.grid(row=5, column=0, columnspan=2, sticky="we", pady=(14, 0))
        make_button(row, "Zapisz", save, "primary").pack(side="left")
        make_button(row, "Folder launchera", lambda: open_folder(ROOT)).pack(side="left", padx=8)
        make_button(row, "Zamknij", d.destroy).pack(side="right")

    def open_console():
        console = state["console"]
        if console and alive(console["win"]):
            console["win"].lift()
            return
        d = make_dialog("Konsola", 860, 480, resizable=True)
        bar = tk.Frame(d, bg=PANEL)
        bar.pack(side="bottom", fill="x")
        text = tk.Text(d, bg=PANEL, fg="#aab2d5", relief="flat", bd=0, padx=10, pady=8, wrap="none",
                       font=("Consolas", -12), insertbackground=TEXT, highlightthickness=0)
        text.pack(fill="both", expand=True)
        if state["log"]:
            text.insert("end", "\n".join(state["log"]) + "\n")
            text.see("end")

        def clear():
            state["log"].clear()
            text.delete("1.0", "end")

        make_button(bar, "Kopiuj wszystko", lambda: copy("\n".join(state["log"]))).pack(side="left", padx=12, pady=8)
        make_button(bar, "Wyczyść", clear).pack(side="left")
        state["console"] = {"win": d, "text": text}

        def close():
            state["console"] = None
            d.destroy()

        d.protocol("WM_DELETE_WINDOW", close)

    # ================================================================== start
    def pump():
        for _ in range(500):  # limit na jedno odswiezenie, zeby okno nie zamarzlo przy duzym logu
            try:
                fn = events.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:  # blad w obsludze zdarzenia nie moze zatrzymac okna
                write_log(f"BŁĄD interfejsu: {type(e).__name__}: {e}")
        root.after(50, pump)

    root.bind("<Return>", lambda e: play())
    refresh_all()
    write_log(f"Folder launchera: {ROOT}")
    background(list_release_versions, lambda versions: state.update(versions=["latest"] + versions),
               lambda msg: write_log(f"Nie udało się pobrać listy wersji: {msg}"))
    if AUTH_FILE.exists() and cfg.get("msa_client_id"):
        login()  # ciche odswiezenie zapisanej sesji; okno z kodem pojawi sie tylko w razie potrzeby
    pump()
    root.mainloop()


if __name__ == "__main__":
    try:
        run_cli() if "--cli" in sys.argv else run_gui()
    except LauncherError as e:
        sys.exit(f"BLAD: {e}")
