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
import os
import platform
import queue
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
            raise LauncherError(f"Microsoft odrzucil aplikacje: {dev.get('error_description', dev)}")
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
        raise LauncherError("Kod logowania wygasl, sprobuj ponownie")

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
            hint = (" Aplikacja Azure nie ma jeszcze zgody Mojangu na API Minecrafta."
                    if code == 403 else "")
            raise LauncherError(f"Minecraft Services odrzucil logowanie ({code}).{hint}")
        try:
            prof = get_json("https://api.minecraftservices.com/minecraft/profile",
                            headers={"Authorization": "Bearer " + mc["access_token"]})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise LauncherError("To konto nie posiada Minecraft: Java Edition")
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
    log("Sprawdzam wersje...")
    v = resolve_version(cfg, log)
    vid = v["_id"]
    log(f"Minecraft {vid}" + (" + Fabric" if v["_fabric"] else ""))
    lib_tasks, classpath = collect_libraries(v)
    client = v["downloads"]["client"]
    client_task = (client["url"], VERSIONS / vid / f"{vid}.jar", client.get("sha1"), client.get("size"))
    log("Pobieram biblioteki i gre...")
    download_all(lib_tasks + [client_task], progress)
    log("Pobieram zasoby...")
    download_all(collect_assets(v), progress, workers=16)
    java = ensure_java(v, cfg, log, progress)
    cmd = build_command(v, account, java, classpath, cfg)
    log(f"Uruchamiam gre jako {account['name']}...")
    return subprocess.Popen(cmd, cwd=GAME_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- interfejsy

def run_cli():
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


def run_gui():
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    events = queue.Queue()
    state = {"account": None, "busy": False}

    win = tk.Tk()
    win.title(f"{NAME} {VERSION}")
    win.geometry("560x420")
    win.configure(bg="#14121f")
    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("TButton", padding=8, font=("Segoe UI", 11))
    style.configure("Horizontal.TProgressbar", troughcolor="#221f33", background="#7c5cff")

    tk.Label(win, text=NAME.upper(), font=("Segoe UI", 28, "bold"), fg="#b9a6ff", bg="#14121f").pack(pady=(18, 0))
    status = tk.Label(win, text="Niezalogowany", font=("Segoe UI", 11), fg="#d8d4ea", bg="#14121f")
    status.pack(pady=(2, 10))
    buttons = tk.Frame(win, bg="#14121f")
    buttons.pack()
    login_btn = ttk.Button(buttons, text="Zaloguj przez Microsoft")
    play_btn = ttk.Button(buttons, text="GRAJ", state="disabled")
    login_btn.grid(row=0, column=0, padx=6)
    play_btn.grid(row=0, column=1, padx=6)
    bar = ttk.Progressbar(win, mode="determinate", length=500)
    bar.pack(pady=12)
    logbox = tk.Text(win, height=11, bg="#0d0c15", fg="#c9c5dc", relief="flat", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def log(msg):
        events.put(("log", str(msg)))

    def progress(done, total):
        events.put(("progress", done, total))

    def worker(fn):
        def run():
            try:
                fn()
            except LauncherError as e:
                log(f"BLAD: {e}")
            except Exception as e:  # siec, nieoczekiwane odpowiedzi API
                log(f"BLAD ({type(e).__name__}): {e}")
            finally:
                events.put(("idle",))
        state["busy"] = True
        login_btn.config(state="disabled")
        play_btn.config(state="disabled")
        threading.Thread(target=run, daemon=True).start()

    def do_login():
        def show(code, uri):
            log(f"Otwieram {uri} - wpisz tam kod: {code}")
            events.put(("code", code))
            webbrowser.open(uri)
        account = MicrosoftAuth(cfg["msa_client_id"]).login(show)
        state["account"] = account
        events.put(("account", account["name"]))

    def do_play():
        proc = prepare_and_launch(cfg, state["account"], log, progress)
        for line in proc.stdout:
            log(line.rstrip())
        log(f"Gra zakonczona (kod {proc.wait()})")

    login_btn.config(command=lambda: worker(do_login))
    play_btn.config(command=lambda: worker(do_play))

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                if ev[0] == "log":
                    logbox.insert("end", ev[1] + "\n")
                    logbox.see("end")
                elif ev[0] == "progress":
                    bar.config(maximum=ev[2], value=ev[1])
                elif ev[0] == "code":
                    status.config(text=f"Kod logowania: {ev[1]}")
                    win.clipboard_clear()
                    win.clipboard_append(ev[1])
                elif ev[0] == "account":
                    status.config(text=f"Zalogowano: {ev[1]}")
                elif ev[0] == "idle":
                    state["busy"] = False
                    login_btn.config(state="normal")
                    play_btn.config(state="normal" if state["account"] else "disabled")
        except queue.Empty:
            pass
        win.after(100, pump)

    log(f"Folder launchera: {ROOT}")
    log(f"Mody wrzucaj do: {GAME_DIR / 'mods'}")
    if AUTH_FILE.exists() and cfg["msa_client_id"]:
        worker(do_login)  # ciche odswiezenie zapisanej sesji
    pump()
    win.mainloop()


if __name__ == "__main__":
    try:
        run_cli() if "--cli" in sys.argv else run_gui()
    except LauncherError as e:
        sys.exit(f"BLAD: {e}")
