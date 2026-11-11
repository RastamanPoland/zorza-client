#!/usr/bin/env python3
"""Zorza Client 0.1 - launcher Minecraft: Java Edition z Fabric Loaderem.

Profile, ustawienia i instalowanie modow z Modrinth oraz CurseForge.

Uruchamianie:   python zorza_launcher.py          (okno)
                python zorza_launcher.py --cli    (terminal)

Wymaga tylko Pythona 3.9+ (biblioteka standardowa). Obsluguje wersje gry
1.19 i nowsze (w tym 26.x). Pliki gry sa pobierane z serwerow Mojangu,
Fabric z meta.fabricmc.net. Logowanie wylacznie kontem Microsoft.
"""
import hashlib
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

NAME = "Zorza Client"
VERSION = "0.2.1"
UA = f"RastamanPoland/zorza-client/{VERSION} (github.com/RastamanPoland/zorza-client)"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net"
FABRIC_META = "https://meta.fabricmc.net/v2"
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

DEFAULT_CONFIG = {
    "msa_client_id": "",      # ID aplikacji z Azure (wymaga zgody Mojangu na API)
    "game_version": "latest",  # "latest" albo np. "26.3"
    "use_fabric": True,
    "memory_mb": 4096,
    "java_path": "",          # puste = pobierz Jave od Mojangu
    "curseforge_api_key": "",  # potrzebny tylko do modow z CurseForge
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
            req = urllib.request.Request(url, headers={"User-Agent": UA})
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


def resolve_version(cfg, log=print) -> dict:
    """Zwraca scalony opis wersji (vanilla + ewentualnie Fabric)."""
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
    v["_id"], v["_fabric"] = vid, None
    if cfg.get("use_fabric"):
        loaders = get_json(f"{FABRIC_META}/versions/loader/{urllib.parse.quote(vid)}")
        if not loaders:
            raise LauncherError(f"Fabric nie wspiera jeszcze wersji {vid}. "
                                "Ustaw starsza 'game_version' albo 'use_fabric': false")
        pick = next((l for l in loaders if l["loader"].get("stable")), loaders[0])
        lv = pick["loader"]["version"]
        log(f"Fabric Loader {lv}")
        prof = get_json(f"{FABRIC_META}/versions/loader/{urllib.parse.quote(vid)}/{lv}/profile/json")
        v["mainClass"] = prof["mainClass"]
        v["libraries"] = v["libraries"] + prof.get("libraries", [])
        for kind in ("jvm", "game"):
            v["arguments"][kind] = v["arguments"].get(kind, []) + prof.get("arguments", {}).get(kind, [])
        v["_fabric"] = lv
    return v


def collect_libraries(v):
    """Zwraca (zadania_pobierania, classpath). Pozniejsze wpisy (Fabric) wygrywaja."""
    chosen = {}
    for lib in v["libraries"]:
        if not rules_allow(lib.get("rules")):
            continue
        art = lib.get("downloads", {}).get("artifact")
        if art:
            path = art.get("path") or maven_path(lib["name"])
            task = (art["url"], LIBS / path, art.get("sha1"), art.get("size"))
        elif "url" in lib:  # styl Fabrica: nazwa + adres repozytorium
            path = maven_path(lib["name"])
            task = (lib["url"].rstrip("/") + "/" + path, LIBS / path, lib.get("sha1"), lib.get("size"))
        else:
            continue
        chosen[lib_key(lib["name"])] = task
    tasks = list(chosen.values())
    return tasks, [t[1] for t in tasks]


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
            time.sleep(interval)
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
    cmd += expand(v["arguments"]["jvm"])
    cmd += [v["mainClass"]]
    cmd += expand(v["arguments"]["game"])
    return cmd


def prepare_and_launch(cfg, account, log=print, progress=None, game_dir=None) -> subprocess.Popen:
    game_dir = game_dir or GAME_DIR
    for d in (LIBS, ASSETS, VERSIONS, RUNTIMES, game_dir / "mods"):
        d.mkdir(parents=True, exist_ok=True)
    log("Sprawdzam wersję…")
    v = resolve_version(cfg, log)
    vid = v["_id"]
    log(f"Minecraft {vid}" + (" + Fabric" if v["_fabric"] else ""))
    lib_tasks, classpath = collect_libraries(v)
    client = v["downloads"]["client"]
    client_task = (client["url"], VERSIONS / vid / f"{vid}.jar", client.get("sha1"), client.get("size"))
    log("Pobieram biblioteki i grę…")
    download_all(lib_tasks + [client_task], progress)
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
    """Profil = osobny folder gry (mody, swiaty, ustawienia) + wersja + Fabric."""
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
            "use_fabric": bool(cfg.get("use_fabric", True)),
        }}}
        save_profiles(data)
    if data.get("selected") not in data["profiles"]:
        data["selected"] = next(iter(data["profiles"]))
    return data


def profile_dir(profile_id: str) -> Path:
    return GAME_DIR if profile_id == "default" else PROFILES_DIR / profile_id


def new_profile_id(name: str, existing) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "profil"
    pid, n = base, 2
    while pid in existing or pid == "default":
        pid, n = f"{base}-{n}", n + 1
    return pid


def valid_game_version(text: str) -> bool:
    return text == "latest" or re.fullmatch(r"\d+(\.\d+){1,2}", text) is not None


def safe_jar_name(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name
    if not name.lower().endswith(".jar") or name.startswith("."):
        raise LauncherError(f"Nieoczekiwana nazwa pliku moda: {name}")
    return name


def mods_index_file(game_dir: Path) -> Path:
    return game_dir / "zorza_mods.json"


def load_mods_index(game_dir: Path) -> dict:
    try:
        return json.loads(mods_index_file(game_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_mods_index(game_dir: Path, index: dict) -> None:
    game_dir.mkdir(parents=True, exist_ok=True)
    mods_index_file(game_dir).write_text(json.dumps(index, indent=2, ensure_ascii=False), "utf-8")


def list_installed(game_dir: Path) -> list:
    """Pliki .jar z folderu mods, z tytulami modow zainstalowanych przez launcher."""
    index = load_mods_index(game_dir)
    mods_dir = game_dir / "mods"
    files = sorted(p.name for p in mods_dir.glob("*.jar")) if mods_dir.is_dir() else []
    return [{"file": f, "title": index.get(f, {}).get("title", f),
             "source": index.get(f, {}).get("source", "ręcznie")} for f in files]


def remove_mod(game_dir: Path, filename: str) -> None:
    name = safe_jar_name(filename)
    path = game_dir / "mods" / name
    if path.exists():
        path.unlink()
    index = load_mods_index(game_dir)
    if index.pop(name, None) is not None:
        save_mods_index(game_dir, index)


def _store_mod(game_dir, url, filename, sha1, size, source, project_id, title, version):
    """Pobiera plik moda, podmienia starsza wersje tego samego moda i zapisuje wpis."""
    if not str(url).startswith("https://"):
        raise LauncherError("Odrzucono adres pobierania, który nie używa HTTPS")
    name = safe_jar_name(filename)
    index = load_mods_index(game_dir)
    for old, info in list(index.items()):
        if info.get("source") == source and str(info.get("id")) == str(project_id) and old != name:
            old_path = game_dir / "mods" / old
            if old_path.exists():
                old_path.unlink()
            del index[old]
    download(url, game_dir / "mods" / name, sha1, size)
    index[name] = {"source": source, "id": str(project_id), "title": title, "version": version}
    save_mods_index(game_dir, index)


def modrinth_search(query: str, game_version: str, limit: int = 30, offset: int = 0) -> list:
    """Bez zapytania zwraca najpopularniejsze mody (sortowanie po liczbie pobran)."""
    facets = [["project_type:mod"], ["categories:fabric"], [f"versions:{game_version}"]]
    params = {"limit": limit, "offset": offset, "facets": json.dumps(facets),
              "index": "relevance" if query else "downloads"}
    if query:
        params["query"] = query
    hits = get_json(f"{MODRINTH}/search?{urllib.parse.urlencode(params)}").get("hits", [])
    return [{"source": "modrinth", "id": h["project_id"], "title": h.get("title", h["project_id"]),
             "description": h.get("description", ""), "downloads": int(h.get("downloads", 0))} for h in hits]


def modrinth_install(project_id, game_version, game_dir, log=print, title=None, _seen=None) -> None:
    seen = _seen if _seen is not None else set()
    if project_id in seen:
        return
    seen.add(project_id)
    pid = urllib.parse.quote(str(project_id), safe="")
    query = urllib.parse.urlencode({"loaders": json.dumps(["fabric"]),
                                    "game_versions": json.dumps([game_version])})
    versions = get_json(f"{MODRINTH}/project/{pid}/version?{query}")
    if title is None:
        title = get_json(f"{MODRINTH}/project/{pid}").get("title", str(project_id))
    if not versions:
        raise LauncherError(f"„{title}” nie ma wersji dla Minecraft {game_version} z Fabric")
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
            modrinth_install(dep_id, game_version, game_dir, log, None, seen)


def curseforge_get(path: str, api_key: str):
    if not api_key:
        raise LauncherError("Brak klucza API CurseForge. Wpisz go w Ustawieniach.")
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
    return [{"source": "curseforge", "id": str(m["id"]), "title": m.get("name", str(m["id"])),
             "description": m.get("summary", ""), "downloads": int(m.get("downloadCount", 0))} for m in data]


def curseforge_install(mod_id, game_version, game_dir, api_key, log=print, title=None, _seen=None) -> None:
    seen = _seen if _seen is not None else set()
    mod_id = int(mod_id)
    if mod_id in seen:
        return
    seen.add(mod_id)
    if title is None:
        title = curseforge_get(f"/mods/{mod_id}", api_key).get("data", {}).get("name", str(mod_id))
    query = urllib.parse.urlencode({"gameVersion": game_version, "modLoaderType": 4, "pageSize": 20})
    files = curseforge_get(f"/mods/{mod_id}/files?{query}", api_key).get("data", [])
    if not files:
        raise LauncherError(f"„{title}” nie ma wersji dla Minecraft {game_version} z Fabric")
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
            curseforge_install(dep["modId"], game_version, game_dir, api_key, log, None, seen)


def install_mod(hit: dict, game_version: str, game_dir: Path, curseforge_key: str = "", log=print) -> None:
    if hit["source"] == "curseforge":
        curseforge_install(hit["id"], game_version, game_dir, curseforge_key, log, hit.get("title"))
    else:
        modrinth_install(hit["id"], game_version, game_dir, log, hit.get("title"))


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
    launch_cfg = dict(cfg, game_version=p["game_version"], use_fabric=bool(p.get("use_fabric", True)))
    proc = prepare_and_launch(launch_cfg, account, print, progress, profile_dir(pdata["selected"]))
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
    from tkinter import messagebox, ttk

    W, H, HEADER, TOP = 760, 640, 250, 48
    BG, PANEL, TEXT, MUTED, LINE = "#0b0d1a", "#11142a", "#e9ecff", "#8088a8", "#1c2040"
    GREEN, GREEN_HI, RED, OFF, SELECT = "#3ddc97", "#6ff0b6", "#ff7a90", "#2a2f4a", "#27407a"

    def font(px, *style):
        return ("Segoe UI", -px) + style  # ujemny rozmiar = piksele, uklad nie zalezy od DPI

    cfg = load_config()
    pdata = load_profiles(cfg)
    events = queue.Queue()
    state = {"account": None, "busy": False, "view": "play", "results": [], "installed": [],
             "results_key": None, "results_req": 0, "results_end": False}
    PAGE = 30

    def profile():
        return pdata["profiles"][pdata["selected"]]

    def current_dir():
        return profile_dir(pdata["selected"])

    win = tk.Tk()
    win.title(f"{NAME} {VERSION}")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.geometry(f"{W}x{H}+{max(0, (win.winfo_screenwidth() - W) // 2)}+{max(0, (win.winfo_screenheight() - H) // 3)}")
    cv = tk.Canvas(win, width=W, height=H, bg=BG, highlightthickness=0, bd=0)
    cv.pack()

    try:  # ciemne listy rozwijane
        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT,
                        arrowcolor=TEXT, bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)])
        for key, value in (("background", PANEL), ("foreground", TEXT),
                           ("selectBackground", SELECT), ("selectForeground", TEXT)):
            win.option_add(f"*TCombobox*Listbox.{key}", value)
    except tk.TclError:
        pass

    # ===================================================== widok GRAJ (canvas)
    try:
        rows = render_aurora(W, HEADER)
        win.aurora = tk.PhotoImage(width=W, height=HEADER)  # referencja, zeby obraz nie zniknal
        win.aurora.put(" ".join("{" + " ".join(r) + "}" for r in rows))
        cv.create_image(0, 0, image=win.aurora, anchor="nw")
    except tk.TclError:
        pass  # bez obrazu zostaje samo ciemne tlo
    cv.create_text(W // 2 + 2, 108, text="Z O R Z A", font=font(58, "bold"), fill="#05060f")
    cv.create_text(W // 2, 106, text="Z O R Z A", font=font(58, "bold"), fill="#ffffff")
    cv.create_text(W // 2, 158, text="C  L  I  E  N  T", font=font(15), fill="#b8f5da")

    profile_item = cv.create_text(W // 2, 226, text="", font=font(14, "bold"), fill="#d7dcff", tags="profile")
    status = cv.create_text(W // 2, 276, text="●  Niezalogowany", font=font(15), fill=MUTED)
    code_text = cv.create_text(W // 2, 312, text="", font=("Consolas", -30, "bold"), fill=GREEN)
    hint = cv.create_text(W // 2, 342, text="", font=font(12), fill=MUTED)

    BX, BY, BW, BH = W // 2, 392, 300, 58
    btn_shape = cv.create_polygon(round_points(BX - BW / 2, BY - BH / 2, BX + BW / 2, BY + BH / 2, 16),
                                  smooth=True, fill=GREEN, outline="", tags="btn")
    btn_label = cv.create_text(BX, BY, text="ZALOGUJ PRZEZ MICROSOFT", font=font(17, "bold"),
                               fill="#06281b", tags="btn")
    logout = cv.create_text(W // 2, 438, text="", font=font(12, "underline"), fill=MUTED, tags="logout")

    PX0, PX1, PY = 120, W - 120, 470
    cv.create_polygon(round_points(PX0, PY - 3, PX1, PY + 3, 3), smooth=True, fill=OFF, outline="")
    bar = cv.create_rectangle(PX0, PY - 3, PX0, PY + 3, fill=GREEN, outline="")
    stage = cv.create_text(W // 2, 490, text="", font=font(12), fill=MUTED, width=W - 160)

    logbox = tk.Text(win, bg=PANEL, fg="#aab2d5", relief="flat", bd=0, padx=10, pady=8, wrap="none",
                     font=("Consolas", -12), insertbackground="#aab2d5", highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=LINE)
    cv.create_window(20, 512, window=logbox, anchor="nw", width=W - 40, height=96)
    footer = cv.create_text(20, H - 14, anchor="w", font=font(11), fill="#565d80", text="")
    cv.create_text(W - 20, H - 14, anchor="e", font=font(11), fill="#565d80", text=f"v{VERSION}")

    # ============================================================ pomocnicze
    def make_button(parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief="flat", bd=0, padx=14, pady=6,
                         cursor="hand2", font=font(13, "bold") if primary else font(13),
                         bg=GREEN if primary else LINE, fg="#06281b" if primary else TEXT,
                         activebackground=GREEN_HI if primary else SELECT,
                         activeforeground="#06281b" if primary else TEXT)

    def make_entry(parent, **kw):
        return tk.Entry(parent, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", font=font(13),
                        highlightthickness=1, highlightbackground=LINE, highlightcolor=GREEN, **kw)

    def make_listbox(parent, height):
        return tk.Listbox(parent, height=height, bg=PANEL, fg=TEXT, selectbackground=SELECT,
                          selectforeground=TEXT, relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=LINE, highlightcolor=LINE, activestyle="none",
                          font=font(13), exportselection=False)

    def make_label(parent, text="", size=13, color=TEXT, bold=False, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=color, anchor="w", justify="left",
                        font=font(size, "bold") if bold else font(size), **kw)

    def set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, value)

    def write_log(msg):
        logbox.insert("end", str(msg) + "\n")
        logbox.see("end")

    def background(job, done, on_error):
        """Uruchamia job() w watku; wynik lub blad wraca do okna przez kolejke."""
        def run():
            try:
                result = job()
            except LauncherError as e:
                events.put(("ui", lambda m=str(e): on_error(m, True)))
            except Exception as e:  # siec, nieoczekiwane odpowiedzi API
                events.put(("ui", lambda m=f"{type(e).__name__}: {e}": on_error(m, True)))
            else:
                events.put(("ui", lambda: done(result)))
        threading.Thread(target=run, daemon=True).start()

    def profile_summary():
        p = profile()
        version = "najnowsza" if p["game_version"] == "latest" else p["game_version"]
        return f"Minecraft {version}" + (" + Fabric" if p.get("use_fabric") else " (bez modów)")

    def refresh_profile_labels():
        cv.itemconfig(profile_item, text=f"Profil: {profile()['name']}  ▾")
        cv.itemconfig(footer, text=profile_summary())
        mods_title.config(text=f"Mody  ·  {profile()['name']}  ·  {profile_summary()}")

    def select_profile(pid):
        if state["busy"] or pid not in pdata["profiles"]:
            return
        pdata["selected"] = pid
        save_profiles(pdata)
        refresh_profile_labels()
        if state["view"] == "mods":
            refresh_installed()
            load_results()
        elif state["view"] == "settings":
            load_settings_fields()

    # ============================================================ widok MODY
    mods = tk.Frame(win, bg=BG)
    mods_title = make_label(mods, size=15, bold=True)
    mods_title.pack(fill="x", padx=24, pady=(14, 8))
    search_row = tk.Frame(mods, bg=BG)
    search_row.pack(fill="x", padx=24)
    source = tk.StringVar(value="modrinth")
    for value, label in (("modrinth", "Modrinth"), ("curseforge", "CurseForge")):
        tk.Radiobutton(search_row, text=label, value=value, variable=source, bg=BG, fg=TEXT,
                       selectcolor=PANEL, activebackground=BG, activeforeground=TEXT, highlightthickness=0,
                       font=font(13), command=lambda: load_results()).pack(side="left", padx=(0, 10))
    query = make_entry(search_row)
    query.pack(side="left", fill="x", expand=True, padx=(6, 10), ipady=5)
    results_lb = make_listbox(mods, 8)
    description = make_label(mods, size=12, color=MUTED, wraplength=W - 48)
    install_row = tk.Frame(mods, bg=BG)
    mods_msg = make_label(install_row, size=12, color=MUTED, wraplength=W - 330)
    installed_lb = make_listbox(mods, 6)
    installed_row = tk.Frame(mods, bg=BG)

    def set_mods_msg(msg, error=False):
        mods_msg.config(text=msg, fg=RED if error else MUTED)
        write_log(msg)

    def refresh_installed():
        state["installed"] = list_installed(current_dir())
        installed_lb.delete(0, "end")
        for m in state["installed"]:
            name = m["title"] if m["title"] == m["file"] else f"{m['title']}   ·   {m['file']}"
            installed_lb.insert("end", name)

    def mods_ready():
        if not profile().get("use_fabric"):
            set_mods_msg("Ten profil nie ma Fabrica, więc mody nie zadziałają. Włącz Fabric w Ustawieniach.", True)
            return False
        if source.get() == "curseforge" and not cfg.get("curseforge_api_key"):
            set_mods_msg("CurseForge wymaga klucza API. Wpisz go w Ustawieniach.", True)
            return False
        return True

    def installed_keys():
        return {(i.get("source"), str(i.get("id"))) for i in load_mods_index(current_dir()).values()}

    def render_results():
        have = installed_keys()
        results_lb.delete(0, "end")
        for h in state["results"]:
            mark = "✓  " if (h["source"], str(h["id"])) in have else "     "
            results_lb.insert("end", f"{mark}{h['title']}   ·   {format_count(h['downloads'])} pobrań")

    def load_results(more=False, force=False):
        """Bez wpisanego tekstu pokazuje najpopularniejsze mody; more=True doczytuje kolejna strone."""
        if not mods_ready():
            return
        text, src, key = query.get().strip(), source.get(), cfg.get("curseforge_api_key", "")
        wanted = profile()["game_version"]
        request_key = (src, pdata["selected"], wanted, text)
        if more and (state["results_key"] != request_key or state["results_end"]):
            return
        if not more and not force and request_key == state["results_key"] and state["results"]:
            return render_results()  # ta sama lista jest juz wczytana
        offset = len(state["results"]) if more else 0
        state["results_req"] += 1
        req = state["results_req"]
        set_mods_msg("Wczytuję kolejne…" if more else ("Szukam…" if text else "Wczytuję najpopularniejsze mody…"))

        def job():
            version = resolve_game_version(wanted)
            if src == "modrinth":
                hits = modrinth_search(text, version, PAGE, offset)
            else:
                hits = curseforge_search(text, version, key, PAGE, offset)
            return version, hits

        def done(result):
            if req != state["results_req"]:
                return  # w miedzyczasie zlecono nowsze wyszukiwanie
            version, hits = result
            if more:
                seen = {(h["source"], str(h["id"])) for h in state["results"]}
                hits = [h for h in hits if (h["source"], str(h["id"])) not in seen]
                state["results"] += hits
            else:
                state["results"] = hits
                description.config(text="")
            state["results_key"], state["results_end"] = request_key, len(hits) < PAGE
            render_results()
            where = "Modrinth" if src == "modrinth" else "CurseForge"
            if not state["results"]:
                set_mods_msg(f"Brak modów dla Minecraft {version} z Fabric")
            elif text:
                set_mods_msg(f"{where}: {len(state['results'])} wyników dla „{text}” (Minecraft {version})")
            else:
                set_mods_msg(f"{where}: najpopularniejsze mody dla Minecraft {version} ({len(state['results'])})")

        def failed(msg, error=True):
            if req == state["results_req"]:
                set_mods_msg(msg, error)

        background(job, done, failed)

    def do_search(_event=None):
        load_results(force=True)

    def selected_index(listbox, items):
        sel = listbox.curselection()
        if not sel or sel[0] >= len(items):
            return None
        return sel[0]

    def on_result_select(_event=None):
        i = selected_index(results_lb, state["results"])
        if i is not None:
            description.config(text=state["results"][i]["description"][:300])

    def do_install():
        i = selected_index(results_lb, state["results"])
        if i is None:
            set_mods_msg("Najpierw zaznacz mod na liście wyników.", True)
            return
        if not mods_ready():
            return
        hit, game_dir, key = state["results"][i], current_dir(), cfg.get("curseforge_api_key", "")
        wanted = profile()["game_version"]
        set_mods_msg(f"Instaluję: {hit['title']}…")

        def job():
            names = []
            install_mod(hit, resolve_game_version(wanted), game_dir, key, names.append)
            return names

        def done(names):
            refresh_installed()
            render_results()
            set_mods_msg("Zainstalowano: " + ", ".join(names))

        def failed(msg, error=True):
            refresh_installed()  # czesc zaleznosci mogla sie juz pobrac
            render_results()
            set_mods_msg(msg, error)

        background(job, done, failed)

    def do_remove():
        i = selected_index(installed_lb, state["installed"])
        if i is None:
            set_mods_msg("Najpierw zaznacz mod na liście zainstalowanych.", True)
            return
        mod = state["installed"][i]
        if not messagebox.askyesno(NAME, f"Usunąć plik {mod['file']} z tego profilu?"):
            return
        try:
            remove_mod(current_dir(), mod["file"])
            set_mods_msg(f"Usunięto: {mod['title']}")
        except (OSError, LauncherError) as e:
            set_mods_msg(f"Nie udało się usunąć: {e}", True)
        refresh_installed()
        render_results()

    make_button(search_row, "Szukaj", do_search, primary=True).pack(side="left")
    query.bind("<Return>", do_search)
    results_lb.pack(fill="x", padx=24, pady=(10, 4))
    results_lb.bind("<<ListboxSelect>>", on_result_select)
    description.pack(fill="x", padx=24)
    install_row.pack(fill="x", padx=24, pady=(6, 6))
    make_button(install_row, "Zainstaluj", do_install, primary=True).pack(side="left")
    make_button(install_row, "Pokaż więcej", lambda: load_results(more=True)).pack(side="left", padx=(8, 0))
    mods_msg.pack(side="left", padx=12)
    make_label(mods, "Zainstalowane w tym profilu", size=13, bold=True).pack(fill="x", padx=24, pady=(8, 4))
    installed_lb.pack(fill="x", padx=24)
    installed_row.pack(fill="x", padx=24, pady=8)
    make_button(installed_row, "Usuń", do_remove).pack(side="left")
    make_button(installed_row, "Otwórz folder mods",
                lambda: open_folder(current_dir() / "mods")).pack(side="left", padx=8)
    make_button(installed_row, "Odśwież", lambda: (refresh_installed(), render_results())).pack(side="left")

    # ====================================================== widok USTAWIENIA
    sett = tk.Frame(win, bg=BG)
    sett.columnconfigure(1, weight=1)
    make_label(sett, "Profil", size=15, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", padx=24, pady=(14, 6))
    profile_cb = ttk.Combobox(sett, state="readonly", font=font(13))
    name_entry = make_entry(sett)
    version_cb = ttk.Combobox(sett, values=["latest"], font=font(13))
    fabric_var = tk.BooleanVar(value=True)
    fabric_check = tk.Checkbutton(sett, text="Fabric (wymagany do modów)", variable=fabric_var, bg=BG, fg=TEXT,
                                  selectcolor=PANEL, activebackground=BG, activeforeground=TEXT,
                                  highlightthickness=0, font=font(13), anchor="w")
    ram_entry, java_entry, cf_entry = make_entry(sett), make_entry(sett), make_entry(sett)
    fields = (("Wybrany profil", profile_cb), ("Nazwa", name_entry),
              ("Wersja gry („latest” = najnowsza)", version_cb), ("", fabric_check))
    for i, (label, widget) in enumerate(fields, start=1):
        make_label(sett, label, color=MUTED).grid(row=i, column=0, sticky="w", padx=(24, 12), pady=4)
        widget.grid(row=i, column=1, sticky="we", padx=(0, 24), pady=4, ipady=3)
    profile_buttons = tk.Frame(sett, bg=BG)
    profile_buttons.grid(row=5, column=0, columnspan=2, sticky="w", padx=24, pady=(6, 10))
    make_label(sett, "Launcher", size=15, bold=True).grid(row=6, column=0, columnspan=2, sticky="w", padx=24, pady=(10, 6))
    fields2 = (("Pamięć RAM dla gry (MB)", ram_entry), ("Ścieżka do Javy (puste = automatycznie)", java_entry),
               ("Klucz API CurseForge (opcjonalny)", cf_entry))
    for i, (label, widget) in enumerate(fields2, start=7):
        make_label(sett, label, color=MUTED).grid(row=i, column=0, sticky="w", padx=(24, 12), pady=4)
        widget.grid(row=i, column=1, sticky="we", padx=(0, 24), pady=4, ipady=3)
    launcher_buttons = tk.Frame(sett, bg=BG)
    launcher_buttons.grid(row=10, column=0, columnspan=2, sticky="w", padx=24, pady=(6, 6))
    sett_msg = make_label(sett, size=12, color=MUTED, wraplength=W - 48)
    sett_msg.grid(row=11, column=0, columnspan=2, sticky="w", padx=24, pady=6)

    def set_sett_msg(msg, error=False):
        sett_msg.config(text=msg, fg=RED if error else GREEN)
        write_log(msg)

    def load_settings_fields():
        ids = list(pdata["profiles"])
        profile_cb.config(values=[pdata["profiles"][pid]["name"] for pid in ids])
        profile_cb.current(ids.index(pdata["selected"]))
        set_entry(name_entry, profile()["name"])
        version_cb.set(profile()["game_version"])
        fabric_var.set(bool(profile().get("use_fabric", True)))
        set_entry(ram_entry, str(cfg.get("memory_mb", 4096)))
        set_entry(java_entry, cfg.get("java_path", ""))
        set_entry(cf_entry, cfg.get("curseforge_api_key", ""))

    def on_profile_pick(_event=None):
        ids = list(pdata["profiles"])
        i = profile_cb.current()
        if isinstance(i, int) and 0 <= i < len(ids):
            select_profile(ids[i])

    def save_profile():
        name = name_entry.get().strip()
        version = version_cb.get().strip() or "latest"
        if not name:
            return set_sett_msg("Nazwa profilu nie może być pusta.", True)
        if not valid_game_version(version):
            return set_sett_msg("Wersja gry musi mieć postać np. 26.3 albo „latest”.", True)
        profile().update({"name": name[:40], "game_version": version, "use_fabric": bool(fabric_var.get())})
        save_profiles(pdata)
        load_settings_fields()
        refresh_profile_labels()
        set_sett_msg(f"Zapisano profil „{profile()['name']}”.")

    def new_profile():
        if state["busy"]:
            return
        name = f"Nowy profil {len(pdata['profiles'])}"
        pid = new_profile_id(name, pdata["profiles"])
        pdata["profiles"][pid] = {"name": name, "game_version": "latest", "use_fabric": True}
        pdata["selected"] = pid
        save_profiles(pdata)
        load_settings_fields()
        refresh_profile_labels()
        set_sett_msg("Utworzono nowy profil. Zmień nazwę i wersję, a potem kliknij „Zapisz profil”.")

    def delete_profile():
        pid = pdata["selected"]
        if state["busy"]:
            return
        if pid == "default":
            return set_sett_msg("Profilu domyślnego nie można usunąć.", True)
        folder = profile_dir(pid)
        if not messagebox.askyesno(NAME, f"Usunąć profil „{profile()['name']}” z listy?\n\n"
                                         f"Pliki (światy, mody) zostaną na dysku w:\n{folder}"):
            return
        del pdata["profiles"][pid]
        pdata["selected"] = "default" if "default" in pdata["profiles"] else next(iter(pdata["profiles"]))
        save_profiles(pdata)
        load_settings_fields()
        refresh_profile_labels()
        set_sett_msg(f"Usunięto profil z listy. Jego pliki zostały w {folder}")

    def save_settings():
        try:
            ram = int(ram_entry.get().strip())
        except ValueError:
            return set_sett_msg("Pamięć RAM musi być liczbą, np. 4096.", True)
        if not 1024 <= ram <= 65536:
            return set_sett_msg("Pamięć RAM musi być w zakresie 1024–65536 MB.", True)
        cfg.update({"memory_mb": ram, "java_path": java_entry.get().strip(),
                    "curseforge_api_key": cf_entry.get().strip()})
        save_config(cfg)
        set_sett_msg("Zapisano ustawienia launchera.")

    profile_cb.bind("<<ComboboxSelected>>", on_profile_pick)
    make_button(profile_buttons, "Zapisz profil", save_profile, primary=True).pack(side="left")
    make_button(profile_buttons, "Nowy profil", new_profile).pack(side="left", padx=8)
    make_button(profile_buttons, "Usuń profil", delete_profile).pack(side="left")
    make_button(profile_buttons, "Otwórz folder profilu", lambda: open_folder(current_dir())).pack(side="left", padx=8)
    make_button(launcher_buttons, "Zapisz ustawienia", save_settings, primary=True).pack(side="left")

    # ============================================================== nawigacja
    nav = {}
    x = 24
    for key, label in (("play", "GRAJ"), ("mods", "MODY"), ("settings", "USTAWIENIA")):
        nav[key] = cv.create_text(x, 24, text=label, anchor="w", font=font(13, "bold"), fill=MUTED,
                                  tags=("nav", "nav_" + key))
        box = cv.bbox(nav[key])
        x = (box[2] if box else x + 12 * len(label)) + 28

    def show(view):
        if state["busy"] and view != "play":
            return  # w trakcie logowania/uruchamiania zostajemy na ekranie gry
        state["view"] = view
        for key, item in nav.items():
            cv.itemconfig(item, fill="#ffffff" if key == view else "#9aa3c7")
        mods.place_forget()
        sett.place_forget()
        if view == "mods":
            refresh_profile_labels()
            refresh_installed()
            mods.place(x=0, y=TOP, width=W, height=H - TOP)
            load_results()  # najpopularniejsze mody od razu po otwarciu zakladki
        elif view == "settings":
            load_settings_fields()
            sett.place(x=0, y=TOP, width=W, height=H - TOP)

    for key in nav:
        cv.tag_bind("nav_" + key, "<Button-1>", lambda e, k=key: show(k))
    cv.tag_bind("nav", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("nav", "<Leave>", lambda e: cv.config(cursor=""))

    def profile_menu(event):
        if state["busy"]:
            return
        menu = tk.Menu(win, tearoff=0, bg=PANEL, fg=TEXT, activebackground=SELECT, activeforeground=TEXT)
        for pid, p in pdata["profiles"].items():
            mark = "✓  " if pid == pdata["selected"] else "     "
            menu.add_command(label=mark + p["name"], command=lambda pid=pid: select_profile(pid))
        menu.add_separator()
        menu.add_command(label="Zarządzaj profilami…", command=lambda: show("settings"))
        menu.tk_popup(event.x_root, event.y_root)

    cv.tag_bind("profile", "<Button-1>", profile_menu)
    cv.tag_bind("profile", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("profile", "<Leave>", lambda e: cv.config(cursor=""))

    # ================================================== logowanie i start gry
    def set_button(text=None, enabled=None):
        if text is not None:
            cv.itemconfig(btn_label, text=text)
        if enabled is not None:
            state["btn_on"] = enabled
            cv.itemconfig(btn_shape, fill=GREEN if enabled else OFF)
            cv.itemconfig(btn_label, fill="#06281b" if enabled else "#6b7294")

    def set_stage(msg, error=False):
        cv.itemconfig(stage, text=msg[:160], fill=RED if error else MUTED)

    def log(msg):
        events.put(("stage", str(msg)))

    def progress(done, total):
        events.put(("progress", done, total))

    def worker(fn, busy_text):
        def run():
            try:
                fn()
            except LauncherError as e:
                events.put(("error", str(e)))
            except Exception as e:  # siec, nieoczekiwane odpowiedzi API
                events.put(("error", f"{type(e).__name__}: {e}"))
            finally:
                events.put(("idle",))
        state["busy"] = True
        set_button(busy_text, False)
        threading.Thread(target=run, daemon=True).start()

    def do_login():
        def show_code(code, uri):
            events.put(("code", code, uri))
            webbrowser.open(uri)
        account = MicrosoftAuth(cfg["msa_client_id"]).login(show_code)
        state["account"] = account
        events.put(("account", account["name"]))

    def on_click(_event=None):
        if state["busy"] or not state.get("btn_on", True) or state["view"] != "play":
            return
        if not state["account"]:
            return worker(do_login, "LOGOWANIE…")
        p, game_dir, account = dict(profile()), current_dir(), state["account"]
        launch_cfg = dict(cfg, game_version=p["game_version"], use_fabric=bool(p.get("use_fabric", True)))

        def do_play():
            proc = prepare_and_launch(launch_cfg, account, log, progress, game_dir)
            events.put(("stage", "Gra uruchomiona"))
            events.put(("ingame",))
            for line in proc.stdout:
                events.put(("gamelog", line.rstrip()))
            events.put(("stage", f"Gra zakończona (kod {proc.wait()})"))

        worker(do_play, "URUCHAMIANIE…")

    def on_logout(_event=None):
        if state["busy"] or not state["account"]:
            return
        try:
            AUTH_FILE.unlink()
        except OSError:
            pass
        state["account"] = None
        cv.itemconfig(status, text="●  Niezalogowany", fill=MUTED)
        cv.itemconfig(logout, text="")
        set_button("ZALOGUJ PRZEZ MICROSOFT", True)
        set_stage("Wylogowano")

    def hover(on):
        if state.get("btn_on", True) and not state["busy"]:
            cv.itemconfig(btn_shape, fill=GREEN_HI if on else GREEN)
            cv.config(cursor="hand2" if on else "")

    cv.tag_bind("btn", "<Button-1>", on_click)
    cv.tag_bind("btn", "<Enter>", lambda e: hover(True))
    cv.tag_bind("btn", "<Leave>", lambda e: hover(False))
    cv.tag_bind("logout", "<Button-1>", on_logout)
    cv.tag_bind("logout", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("logout", "<Leave>", lambda e: cv.config(cursor=""))

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                kind = ev[0]
                if kind == "ui":
                    ev[1]()
                elif kind == "stage":
                    set_stage(ev[1])
                    write_log(ev[1])
                elif kind == "gamelog":
                    write_log(ev[1])
                elif kind == "error":
                    set_stage("Błąd: " + ev[1].splitlines()[0], error=True)
                    write_log("BŁĄD: " + ev[1])
                    cv.itemconfig(code_text, text="")
                    cv.itemconfig(hint, text="")
                elif kind == "progress":
                    frac = ev[1] / ev[2] if ev[2] else 0
                    cv.coords(bar, PX0, PY - 3, PX0 + (PX1 - PX0) * frac, PY + 3)
                elif kind == "code":
                    cv.itemconfig(code_text, text=ev[1])
                    cv.itemconfig(hint, text=f"Wpisz ten kod na {ev[2]} (jest już w schowku)")
                    write_log(f"Kod logowania: {ev[1]}  ->  {ev[2]}")
                    win.clipboard_clear()
                    win.clipboard_append(ev[1])
                elif kind == "account":
                    cv.itemconfig(status, text=f"●  Zalogowano: {ev[1]}", fill=GREEN)
                    cv.itemconfig(code_text, text="")
                    cv.itemconfig(hint, text="")
                    cv.itemconfig(logout, text="Wyloguj")
                    set_stage("Gotowe do gry")
                elif kind == "ingame":
                    cv.coords(bar, PX0, PY - 3, PX1, PY + 3)
                    set_button("W GRZE", False)
                elif kind == "idle":
                    state["busy"] = False
                    cv.coords(bar, PX0, PY - 3, PX0, PY + 3)
                    set_button("GRAJ" if state["account"] else "ZALOGUJ PRZEZ MICROSOFT", True)
        except queue.Empty:
            pass
        win.after(100, pump)

    refresh_profile_labels()
    show("play")
    write_log(f"Folder launchera: {ROOT}")
    background(list_release_versions, lambda versions: version_cb.config(values=["latest"] + versions),
               lambda msg, error=True: write_log(f"Nie udało się pobrać listy wersji: {msg}"))
    if AUTH_FILE.exists() and cfg["msa_client_id"]:
        worker(do_login, "LOGOWANIE…")  # ciche odswiezenie zapisanej sesji
    pump()
    win.mainloop()


if __name__ == "__main__":
    try:
        run_cli() if "--cli" in sys.argv else run_gui()
    except LauncherError as e:
        sys.exit(f"BLAD: {e}")
