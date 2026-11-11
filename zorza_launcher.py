#!/usr/bin/env python3
"""Zorza Client 0.1 - launcher Minecraft: Java Edition z Fabric Loaderem.

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
VERSION = "0.1.0"
UA = f"ZorzaClient/{VERSION}"

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

DEFAULT_CONFIG = {
    "msa_client_id": "",      # ID aplikacji z Azure (wymaga zgody Mojangu na API)
    "game_version": "latest",  # "latest" albo np. "26.3"
    "use_fabric": True,
    "memory_mb": 4096,
    "java_path": "",          # puste = pobierz Jave od Mojangu
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

def build_command(v, account, java, classpath, cfg) -> list:
    vid = v["_id"]
    client_jar = VERSIONS / vid / f"{vid}.jar"
    natives = VERSIONS / vid / "natives"
    natives.mkdir(parents=True, exist_ok=True)
    values = {
        "auth_player_name": account["name"], "auth_uuid": account["uuid"],
        "auth_access_token": account["token"], "auth_xuid": "", "clientid": "",
        "user_type": "msa", "user_properties": "{}",
        "version_name": vid, "version_type": v.get("type", "release"),
        "game_directory": str(GAME_DIR), "assets_root": str(ASSETS),
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


def prepare_and_launch(cfg, account, log=print, progress=None) -> subprocess.Popen:
    for d in (LIBS, ASSETS, VERSIONS, RUNTIMES, GAME_DIR / "mods"):
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
    cmd = build_command(v, account, java, classpath, cfg)
    log(f"Uruchamiam grę jako {account['name']}…")
    return subprocess.Popen(cmd, cwd=GAME_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")


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

    proc = prepare_and_launch(cfg, account, print, progress)
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

    W, H, HEADER = 760, 640, 250
    BG, PANEL, TEXT, MUTED = "#0b0d1a", "#11142a", "#e9ecff", "#8088a8"
    GREEN, GREEN_HI, RED, OFF = "#3ddc97", "#6ff0b6", "#ff7a90", "#2a2f4a"

    def font(px, *style):
        return ("Segoe UI", -px) + style  # ujemny rozmiar = piksele, uklad nie zalezy od DPI

    cfg = load_config()
    events = queue.Queue()
    state = {"account": None, "busy": False}

    win = tk.Tk()
    win.title(f"{NAME} {VERSION}")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.geometry(f"{W}x{H}+{max(0, (win.winfo_screenwidth() - W) // 2)}+{max(0, (win.winfo_screenheight() - H) // 3)}")
    cv = tk.Canvas(win, width=W, height=H, bg=BG, highlightthickness=0, bd=0)
    cv.pack()

    # --- naglowek z zorza
    try:
        rows = render_aurora(W, HEADER)
        win.aurora = tk.PhotoImage(width=W, height=HEADER)  # referencja, zeby obraz nie zniknal
        win.aurora.put(" ".join("{" + " ".join(r) + "}" for r in rows))
        cv.create_image(0, 0, image=win.aurora, anchor="nw")
    except tk.TclError:
        pass  # bez obrazu zostaje samo ciemne tlo
    cv.create_text(W // 2 + 2, 98, text="Z O R Z A", font=font(58, "bold"), fill="#05060f")
    cv.create_text(W // 2, 96, text="Z O R Z A", font=font(58, "bold"), fill="#ffffff")
    cv.create_text(W // 2, 148, text="C  L  I  E  N  T", font=font(15), fill="#b8f5da")

    # --- stan konta i kod logowania
    status = cv.create_text(W // 2, 276, text="●  Niezalogowany", font=font(15), fill=MUTED)
    code_text = cv.create_text(W // 2, 312, text="", font=("Consolas", -30, "bold"), fill=GREEN)
    hint = cv.create_text(W // 2, 342, text="", font=font(12), fill=MUTED)

    # --- glowny przycisk
    BX, BY, BW, BH = W // 2, 392, 300, 58
    btn_shape = cv.create_polygon(round_points(BX - BW / 2, BY - BH / 2, BX + BW / 2, BY + BH / 2, 16),
                                  smooth=True, fill=GREEN, outline="", tags="btn")
    btn_label = cv.create_text(BX, BY, text="ZALOGUJ PRZEZ MICROSOFT", font=font(17, "bold"),
                               fill="#06281b", tags="btn")
    logout = cv.create_text(W // 2, 438, text="", font=font(12, "underline"), fill=MUTED, tags="logout")

    # --- pasek postepu i etap
    PX0, PX1, PY = 120, W - 120, 470
    cv.create_polygon(round_points(PX0, PY - 3, PX1, PY + 3, 3), smooth=True, fill=OFF, outline="")
    bar = cv.create_rectangle(PX0, PY - 3, PX0, PY + 3, fill=GREEN, outline="")
    stage = cv.create_text(W // 2, 490, text="", font=font(12), fill=MUTED, width=W - 160)

    # --- log
    logbox = tk.Text(win, bg=PANEL, fg="#aab2d5", relief="flat", bd=0, padx=10, pady=8, wrap="none",
                     font=("Consolas", -12), insertbackground="#aab2d5", highlightthickness=1,
                     highlightbackground="#1c2040", highlightcolor="#1c2040")
    cv.create_window(20, 512, window=logbox, anchor="nw", width=W - 40, height=96)
    version_label = "najnowsza" if cfg["game_version"] == "latest" else cfg["game_version"]
    cv.create_text(20, H - 14, anchor="w", font=font(11), fill="#565d80",
                   text=f"Minecraft {version_label}" + (" + Fabric" if cfg.get("use_fabric") else ""))
    cv.create_text(W - 20, H - 14, anchor="e", font=font(11), fill="#565d80", text=f"v{VERSION}")

    def set_button(text=None, enabled=None):
        if text is not None:
            cv.itemconfig(btn_label, text=text)
        if enabled is not None:
            state["btn_on"] = enabled
            cv.itemconfig(btn_shape, fill=GREEN if enabled else OFF)
            cv.itemconfig(btn_label, fill="#06281b" if enabled else "#6b7294")

    def set_stage(msg, error=False):
        cv.itemconfig(stage, text=msg[:160], fill=RED if error else MUTED)

    def write_log(msg):
        logbox.insert("end", msg + "\n")
        logbox.see("end")

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
        def show(code, uri):
            events.put(("code", code, uri))
            webbrowser.open(uri)
        account = MicrosoftAuth(cfg["msa_client_id"]).login(show)
        state["account"] = account
        events.put(("account", account["name"]))

    def do_play():
        proc = prepare_and_launch(cfg, state["account"], log, progress)
        events.put(("stage", "Gra uruchomiona"))
        events.put(("ingame",))
        for line in proc.stdout:
            events.put(("gamelog", line.rstrip()))
        events.put(("stage", f"Gra zakończona (kod {proc.wait()})"))

    def on_click(_event=None):
        if state["busy"] or not state.get("btn_on", True):
            return
        if state["account"]:
            worker(do_play, "URUCHAMIANIE…")
        else:
            worker(do_login, "LOGOWANIE…")

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
    win.bind("<Return>", on_click)

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                kind = ev[0]
                if kind == "stage":
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

    write_log(f"Folder launchera: {ROOT}")
    write_log(f"Mody wrzucaj do: {GAME_DIR / 'mods'}")
    if AUTH_FILE.exists() and cfg["msa_client_id"]:
        worker(do_login, "LOGOWANIE…")  # ciche odswiezenie zapisanej sesji
    pump()
    win.mainloop()


if __name__ == "__main__":
    try:
        run_cli() if "--cli" in sys.argv else run_gui()
    except LauncherError as e:
        sys.exit(f"BLAD: {e}")
