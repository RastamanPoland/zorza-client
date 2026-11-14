<script>
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import { onMount } from "svelte";

  const LOADERS = [
    ["vanilla", "Vanilla (bez modów)"],
    ["fabric", "Fabric"],
    ["quilt", "Quilt"],
    ["forge", "Forge"],
    ["neoforge", "NeoForge"],
  ];
  const LOADER_NAME = Object.fromEntries(LOADERS);
  const PALETTE = ["#3ddc97", "#2bc4c9", "#8b5cf6", "#d16ba5", "#f2a65a", "#5b8def"];

  let info = $state(null);
  let status = $state({ text: "Uruchamianie…", error: false });
  let progress = $state(0);
  let versions = $state(["latest"]);
  let data = $state({ selected: "default", profiles: {} });
  let config = $state({ memory_mb: 4096, java_path: "", curseforge_api_key: "" });

  // gra
  let busyPid = $state(null); // profil, który się uruchamia
  let runningPid = $state(null); // profil, w którym działa gra
  let log = $state([]);
  let consoleOpen = $state(false);

  // logowanie
  let account = $state(null);
  let loggingIn = $state(false);
  let loginCode = $state(null);
  let menu = $state(null); // "account" | "folders" | null
  let playAfterLogin = $state(null);

  // dialogi
  let dialog = $state(null); // { kind: "add" | "edit" | "settings", form, error }

  let selected = $derived(data.profiles[data.selected]);
  let entries = $derived(Object.entries(data.profiles));
  let busy = $derived(busyPid !== null || runningPid !== null);

  function setStatus(text, error = false) {
    status = { text, error };
    addLog((error ? "BŁĄD: " : "") + text);
  }

  function addLog(line) {
    log.push(line);
    if (log.length > 4000) log.splice(0, log.length - 4000);
  }

  function color(pid) {
    let sum = 0;
    for (const ch of pid) sum += ch.charCodeAt(0);
    return PALETTE[sum % PALETTE.length];
  }

  function versionLabel(p) {
    const v = p.game_version === "latest" ? "najnowsza" : p.game_version;
    return p.loader && p.loader !== "vanilla" ? `${v} · ${LOADER_NAME[p.loader]}` : v;
  }

  function newId(name) {
    const base =
      name
        .toLowerCase()
        .normalize("NFD")
        .replace(/[̀-ͯ]/g, "")
        .replace(/ł/g, "l")
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 40) || "profil";
    let id = base;
    for (let n = 2; id === "default" || data.profiles[id]; n++) id = `${base}-${n}`;
    return id;
  }

  const validVersion = (v) => v === "latest" || /^\d+(\.\d+){1,2}$/.test(v);

  async function saveProfiles() {
    try {
      await invoke("save_profiles", { data: $state.snapshot(data) });
    } catch (e) {
      setStatus(String(e), true);
    }
  }

  function select(pid) {
    if (data.selected !== pid) {
      data.selected = pid;
      saveProfiles();
    }
  }

  // ------------------------------------------------------------- logowanie
  async function login() {
    if (loggingIn) return;
    menu = null;
    loggingIn = true;
    setStatus("Logowanie…");
    try {
      account = await invoke("login");
      setStatus(`Zalogowano jako ${account.name}`);
      if (playAfterLogin) {
        const pid = playAfterLogin;
        playAfterLogin = null;
        play(pid);
      }
    } catch (e) {
      playAfterLogin = null;
      setStatus(String(e), true);
    } finally {
      loggingIn = false;
      loginCode = null;
    }
  }

  async function cancelLogin() {
    loginCode = null;
    await invoke("cancel_login");
  }

  async function logout() {
    menu = null;
    await invoke("logout");
    account = null;
    setStatus("Wylogowano");
  }

  function copyCode() {
    navigator.clipboard?.writeText(loginCode.code).catch(() => {});
  }

  // ------------------------------------------------------------------- gra
  async function play(pid = data.selected) {
    if (busy) return setStatus("Gra już działa albo właśnie się uruchamia.", true);
    if (!account) {
      playAfterLogin = pid;
      return login();
    }
    busyPid = pid;
    progress = 0;
    try {
      await invoke("launch", { pid });
    } catch (e) {
      busyPid = null;
      progress = 0;
      setStatus(String(e), true);
    }
  }

  async function stop() {
    try {
      await invoke("stop_game");
      setStatus("Zatrzymuję grę…");
    } catch (e) {
      setStatus(String(e), true);
    }
  }

  // ---------------------------------------------------------------- profile
  function openAdd() {
    dialog = {
      kind: "add",
      error: "",
      form: { name: `Profil ${entries.length + 1}`, game_version: "latest", loader: "fabric" },
    };
  }

  function openEdit(pid = data.selected) {
    const p = data.profiles[pid];
    dialog = {
      kind: "edit",
      pid,
      error: "",
      form: {
        name: p.name,
        game_version: p.game_version,
        loader: p.loader || "fabric",
        loader_version: p.loader_version || "",
        memory_mb: p.memory_mb ? String(p.memory_mb) : "",
        jvm_args: p.jvm_args || "",
      },
    };
  }

  function openSettings() {
    dialog = {
      kind: "settings",
      error: "",
      form: {
        memory_mb: String(config.memory_mb ?? 4096),
        java_path: config.java_path || "",
        curseforge_api_key: config.curseforge_api_key || "",
      },
    };
  }

  function submitAdd() {
    const f = dialog.form;
    const name = f.name.trim().slice(0, 40);
    const version = f.game_version.trim() || "latest";
    if (!name) return (dialog.error = "Podaj nazwę profilu.");
    if (!validVersion(version)) return (dialog.error = "Wersja gry musi mieć postać np. 1.21.1 albo „latest”.");
    const pid = newId(name);
    data.profiles[pid] = { name, game_version: version, loader: f.loader };
    data.selected = pid;
    dialog = null;
    saveProfiles();
    setStatus(`Utworzono profil „${name}”`);
  }

  function submitEdit() {
    const f = dialog.form;
    const pid = dialog.pid;
    const name = f.name.trim().slice(0, 40);
    const version = f.game_version.trim() || "latest";
    if (busyPid === pid || runningPid === pid) return (dialog.error = "Zamknij grę przed zmianą profilu.");
    if (!name) return (dialog.error = "Nazwa profilu nie może być pusta.");
    if (!validVersion(version)) return (dialog.error = "Wersja gry musi mieć postać np. 1.21.1 albo „latest”.");
    const pin = f.loader_version.trim();
    if (pin && !/^[0-9A-Za-z.+_-]{1,60}$/.test(pin))
      return (dialog.error = "Wersja loadera może zawierać tylko litery, cyfry i znaki . + _ -");
    let ram = null;
    if (f.memory_mb.trim()) {
      ram = Number(f.memory_mb.trim());
      if (!Number.isInteger(ram) || ram < 1024 || ram > 65536)
        return (dialog.error = "Pamięć RAM musi być liczbą 1024–65536 albo pusta.");
    }
    const p = { ...$state.snapshot(data.profiles[pid]), name, game_version: version, loader: f.loader };
    const oldLoader = data.profiles[pid].loader;
    if (pin && f.loader !== "vanilla" && !(f.loader !== oldLoader && pin === data.profiles[pid].loader_version))
      p.loader_version = pin;
    else delete p.loader_version;
    if (ram) p.memory_mb = ram;
    else delete p.memory_mb;
    if (f.jvm_args.trim()) p.jvm_args = f.jvm_args.trim().slice(0, 500);
    else delete p.jvm_args;
    data.profiles[pid] = p;
    dialog = null;
    saveProfiles();
    setStatus(`Zapisano profil „${name}”`);
  }

  async function submitSettings() {
    const f = dialog.form;
    const ram = Number(f.memory_mb.trim());
    if (!Number.isInteger(ram) || ram < 1024 || ram > 65536)
      return (dialog.error = "Pamięć RAM musi być liczbą 1024–65536.");
    const update = { memory_mb: ram, java_path: f.java_path.trim(), curseforge_api_key: f.curseforge_api_key.trim() };
    try {
      await invoke("save_config", { update });
      config = { ...config, ...update };
      dialog = null;
      setStatus("Zapisano ustawienia");
    } catch (e) {
      dialog.error = String(e);
    }
  }

  async function copyProfile() {
    const pid = data.selected;
    if (busyPid === pid || runningPid === pid) return setStatus("Zamknij grę przed kopiowaniem profilu.", true);
    const p = $state.snapshot(data.profiles[pid]);
    const name = `${p.name} (kopia)`.slice(0, 40);
    const dst = newId(name);
    setStatus(`Kopiuję profil „${p.name}”…`);
    try {
      await invoke("copy_profile", { src: pid, dst });
      data.profiles[dst] = { ...p, name };
      data.selected = dst;
      await saveProfiles();
      setStatus(`Skopiowano profil jako „${name}”`);
    } catch (e) {
      setStatus(String(e), true);
    }
  }

  function deleteProfile() {
    const pid = data.selected;
    if (pid === "default") return setStatus("Profilu domyślnego nie można usunąć.", true);
    if (busyPid === pid || runningPid === pid) return setStatus("Zamknij grę przed usunięciem profilu.", true);
    const name = data.profiles[pid].name;
    if (!confirm(`Usunąć profil „${name}” z listy?\n\nPliki (światy, mody) zostaną na dysku.`)) return;
    delete data.profiles[pid];
    data.selected = data.profiles.default ? "default" : Object.keys(data.profiles)[0];
    saveProfiles();
    setStatus(`Usunięto profil „${name}” z listy. Jego pliki zostały na dysku.`);
  }

  async function openFolder(pid, sub = null) {
    menu = null;
    try {
      await invoke("open_folder", { pid, sub });
    } catch (e) {
      setStatus(String(e), true);
    }
  }

  function onKey(e) {
    if (e.key === "Escape") {
      if (dialog) dialog = null;
      else if (menu) menu = null;
    } else if (e.key === "Enter" && dialog) {
      e.preventDefault();
      ({ add: submitAdd, edit: submitEdit, settings: submitSettings })[dialog.kind]();
    }
  }

  onMount(() => {
    const unlisteners = [
      listen("login-code", (e) => {
        loginCode = e.payload;
        copyCode();
      }),
      listen("status", (e) => setStatus(e.payload)),
      listen("progress", (e) => {
        const [done, total] = e.payload;
        progress = total ? done / total : 0;
      }),
      listen("game-log", (e) => addLog(e.payload)),
      listen("game-started", (e) => {
        runningPid = e.payload;
        busyPid = null;
        progress = 1;
        setStatus(`Gra uruchomiona (${data.profiles[e.payload]?.name ?? e.payload})`);
      }),
      listen("game-exit", (e) => {
        runningPid = null;
        busyPid = null;
        progress = 0;
        setStatus(`Gra zakończona (kod ${e.payload ?? "?"})`, e.payload !== 0 && e.payload !== null);
      }),
    ];
    (async () => {
      try {
        info = await invoke("app_info");
        data = await invoke("get_profiles");
        config = await invoke("get_config");
        addLog(`Folder launchera: ${info.data_dir}`);
        setStatus("Gotowe");
        if (await invoke("has_saved_login")) login(); // ciche odświeżenie zapisanej sesji
        versions = ["latest", ...(await invoke("list_versions"))];
      } catch (e) {
        setStatus(String(e), true);
      }
    })();
    return () => unlisteners.forEach((u) => u.then((fn) => fn()));
  });
</script>

<svelte:window onkeydown={onKey} onclick={() => (menu = null)} />

<datalist id="versions">
  {#each versions as v}<option value={v}></option>{/each}
</datalist>

<div class="window">
  <header class="banner">
    <div class="aurora a1"></div>
    <div class="aurora a2"></div>
    <div class="aurora a3"></div>
    <div class="logo">ZORZA <span>CLIENT</span></div>
  </header>

  <nav class="toolbar">
    <button onclick={openAdd}>＋ Dodaj profil</button>
    <button disabled title="Wkrótce w nowej wersji">Modpacki…</button>
    <div class="menu-wrap">
      <button onclick={(e) => { e.stopPropagation(); menu = menu === "folders" ? null : "folders"; }}>Foldery ▾</button>
      {#if menu === "folders"}
        <div class="menu left">
          <button class="menu-item" onclick={() => openFolder("")}>Folder launchera</button>
          <button class="menu-item" onclick={() => openFolder(data.selected)}>Folder profilu</button>
          <button class="menu-item" onclick={() => openFolder(data.selected, "mods")}>Folder mods</button>
        </div>
      {/if}
    </div>
    <button onclick={openSettings}>Ustawienia</button>
    <button class:active={consoleOpen} onclick={() => (consoleOpen = !consoleOpen)}>Konsola</button>
    <div class="spacer"></div>
    <div class="menu-wrap">
      <button
        onclick={(e) => { e.stopPropagation(); account ? (menu = menu === "account" ? null : "account") : login(); }}
        disabled={loggingIn && !loginCode}
      >
        {#if loggingIn}Logowanie…{:else if account}● {account.name} ▾{:else}Zaloguj się{/if}
      </button>
      {#if menu === "account" && account}
        <div class="menu">
          <div class="menu-item muted">Zalogowano: {account.name}</div>
          <button class="menu-item" onclick={logout}>Wyloguj</button>
        </div>
      {/if}
    </div>
  </nav>

  <main class="body">
    <section class="tiles">
      {#each entries as [pid, p] (pid)}
        <button
          class="tile"
          class:selected={pid === data.selected}
          onclick={() => select(pid)}
          ondblclick={() => play(pid)}
          oncontextmenu={(e) => { e.preventDefault(); select(pid); openEdit(pid); }}
        >
          <div class="icon" style:background={color(pid)}>{(p.name[0] || "?").toUpperCase()}</div>
          <div class="name">{p.name}</div>
          <div class="sub" class:live={pid === busyPid || pid === runningPid}>
            {#if pid === runningPid}▶ w grze{:else if pid === busyPid}uruchamianie…{:else}{versionLabel(p)}{/if}
          </div>
        </button>
      {/each}
    </section>

    {#if selected}
      <aside class="side">
        <div class="big-icon" style:background={color(data.selected)}>{(selected.name[0] || "?").toUpperCase()}</div>
        <div class="side-name">{selected.name}</div>
        <div class="muted">Minecraft {versionLabel(selected)}</div>
        <button class="primary wide" onclick={() => play()} disabled={busy}>
          {#if busyPid}Uruchamianie…{:else if runningPid}▶ W grze{:else}▶ Graj{/if}
        </button>
        <button class="danger full" onclick={stop} disabled={runningPid !== data.selected}>■ Zatrzymaj</button>
        <div class="gap"></div>
        <button class="full" onclick={() => openEdit()}>Edytuj profil…</button>
        <button class="full" disabled title="Wkrótce w nowej wersji">Pobierz mody i paczki…</button>
        <button class="full" onclick={() => openFolder(data.selected)}>Folder profilu</button>
        <button class="full" onclick={copyProfile}>Kopiuj profil</button>
        <button
          class="danger full"
          onclick={deleteProfile}
          disabled={data.selected === "default" || busyPid === data.selected || runningPid === data.selected}
        >Usuń profil</button>
      </aside>
    {/if}
  </main>

  {#if consoleOpen}
    <section class="console">
      <div class="console-bar">
        <button onclick={() => navigator.clipboard?.writeText(log.join("\n"))}>Kopiuj wszystko</button>
        <button onclick={() => (log = [])}>Wyczyść</button>
      </div>
      <pre>{log.slice(-600).join("\n")}</pre>
    </section>
  {/if}

  <footer class="status">
    <span class="status-text" class:err={status.error}>{status.text}</span>
    <div class="bar"><div class="fill" style:width="{Math.round(progress * 100)}%"></div></div>
  </footer>
</div>

{#if loginCode}
  <div class="overlay">
    <div class="dialog center">
      <h2>Zaloguj się kontem Microsoft</h2>
      <p class="muted">Wejdź na {loginCode.uri} i wpisz kod:</p>
      <div class="code">{loginCode.code}</div>
      <p class="muted small">Kod jest już w schowku. Okno zamknie się samo po zalogowaniu.</p>
      <div class="row">
        <button class="primary" onclick={() => invoke("open_url", { url: loginCode.uri })}>Otwórz stronę</button>
        <button onclick={copyCode}>Kopiuj kod</button>
        <button onclick={cancelLogin}>Anuluj</button>
      </div>
    </div>
  </div>
{/if}

{#if dialog}
  <div class="overlay">
    <div class="dialog">
      {#if dialog.kind === "add"}
        <h2>Nowy profil</h2>
        <label>Nazwa <input bind:value={dialog.form.name} maxlength="40" /></label>
        <label>Wersja gry <input bind:value={dialog.form.game_version} list="versions" /></label>
        <label>Loader modów
          <select bind:value={dialog.form.loader}>
            {#each LOADERS as [id, label]}<option value={id}>{label}</option>{/each}
          </select>
        </label>
      {:else if dialog.kind === "edit"}
        <h2>Profil – {data.profiles[dialog.pid]?.name}</h2>
        <label>Nazwa <input bind:value={dialog.form.name} maxlength="40" /></label>
        <label>Wersja gry („latest” = najnowsza) <input bind:value={dialog.form.game_version} list="versions" /></label>
        <label>Loader modów
          <select bind:value={dialog.form.loader}>
            {#each LOADERS as [id, label]}<option value={id}>{label}</option>{/each}
          </select>
        </label>
        <label>Wersja loadera (puste = najnowsza)
          <input bind:value={dialog.form.loader_version} disabled={dialog.form.loader === "vanilla"} />
        </label>
        <label>Pamięć RAM w MB (puste = {config.memory_mb} z ustawień)
          <input bind:value={dialog.form.memory_mb} inputmode="numeric" />
        </label>
        <label>Dodatkowe argumenty JVM <input bind:value={dialog.form.jvm_args} /></label>
        {#if dialog.form.loader === "forge" || dialog.form.loader === "neoforge"}
          <p class="muted small">Forge i NeoForge będą działać w nowej wersji w kolejnym kroku.</p>
        {/if}
      {:else}
        <h2>Ustawienia launchera</h2>
        <label>Pamięć RAM dla gry (MB) <input bind:value={dialog.form.memory_mb} inputmode="numeric" /></label>
        <label>Ścieżka do Javy (puste = pobierz od Mojangu) <input bind:value={dialog.form.java_path} /></label>
        <label>Własny klucz API CurseForge <input type="password" bind:value={dialog.form.curseforge_api_key} /></label>
      {/if}
      {#if dialog.error}<p class="error">{dialog.error}</p>{/if}
      <div class="row end">
        <button onclick={() => (dialog = null)}>Anuluj</button>
        <button class="primary" onclick={() => ({ add: submitAdd, edit: submitEdit, settings: submitSettings })[dialog.kind]()}>
          {dialog.kind === "add" ? "Utwórz" : "Zapisz"}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .window { display: flex; flex-direction: column; height: 100%; }
  .banner { position: relative; height: 64px; overflow: hidden; flex: none; background: linear-gradient(180deg, #04060f, var(--bg)); }
  .aurora {
    position: absolute; left: -10%; width: 120%; height: 36px; border-radius: 50%;
    filter: blur(14px); opacity: 0.75; animation: drift 14s ease-in-out infinite alternate;
  }
  .a1 { top: 30px; background: linear-gradient(90deg, transparent, #3ddc97, #2bc4c9, transparent); }
  .a2 { top: 18px; background: linear-gradient(90deg, #2bc4c9, transparent, #3ddc97, transparent); animation-duration: 18s; opacity: 0.5; }
  .a3 { top: 6px; background: linear-gradient(90deg, transparent, #8b5cf6, transparent, #8b5cf6); animation-duration: 22s; opacity: 0.45; }
  @keyframes drift { from { transform: translateX(-4%) skewX(-8deg); } to { transform: translateX(4%) skewX(8deg); } }
  .logo { position: relative; padding: 14px 24px; font-size: 28px; font-weight: 800; letter-spacing: 1px; text-shadow: 0 0 18px rgba(61, 220, 151, 0.45); }
  .logo span { font-size: 12px; font-weight: 400; color: #b8f5da; letter-spacing: 2px; }

  .toolbar { display: flex; gap: 6px; padding: 7px 10px; background: var(--panel); flex: none; }
  .toolbar .active { background: var(--select); }
  .spacer { flex: 1; }
  .menu-wrap { position: relative; }
  .menu {
    position: absolute; right: 0; top: calc(100% + 4px); min-width: 200px; z-index: 10;
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 4px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }
  .menu.left { left: 0; right: auto; }
  .menu-item { display: block; width: 100%; text-align: left; padding: 8px 10px; background: none; border-radius: 6px; }

  .body { flex: 1; display: flex; min-height: 0; }
  .tiles { flex: 1; padding: 16px; display: flex; flex-wrap: wrap; align-content: flex-start; gap: 8px; overflow: auto; }
  .tile { width: 128px; padding: 12px 6px; border-radius: 12px; text-align: center; background: none; }
  .tile:hover { background: var(--panel2); }
  .tile.selected { background: var(--select); }
  .icon, .big-icon { margin: 0 auto; display: grid; place-items: center; font-weight: 800; color: var(--bg); }
  .icon { width: 64px; height: 64px; font-size: 30px; border-radius: 16px; }
  .big-icon { width: 76px; height: 76px; font-size: 36px; margin-top: 22px; border-radius: 18px; }
  .name { margin-top: 10px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sub, .muted { color: var(--muted); font-size: 12px; }
  .sub.live { color: var(--green); }

  .side { width: 250px; background: var(--panel); display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 0 18px; flex: none; overflow: auto; }
  .side-name { font-size: 17px; font-weight: 700; margin-top: 6px; text-align: center; }
  .wide { width: 100%; margin-top: 12px; padding: 10px; }
  .full { width: 100%; }
  .gap { height: 8px; }

  .console { height: 220px; flex: none; display: flex; flex-direction: column; background: #080a14; border-top: 1px solid var(--line); }
  .console-bar { display: flex; gap: 6px; padding: 6px 10px; }
  .console pre {
    flex: 1; margin: 0; padding: 4px 12px 8px; overflow: auto; color: #aab2d5; user-select: text;
    font: 12px/1.45 Consolas, "Cascadia Mono", monospace;
  }

  .status { background: var(--panel); color: var(--muted); font-size: 12px; padding: 6px 12px; flex: none; display: flex; align-items: center; gap: 12px; }
  .status-text { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; user-select: text; }
  .status-text.err { color: var(--red); }
  .bar { width: 220px; height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .fill { height: 100%; background: var(--green); transition: width 0.2s; }

  .overlay { position: fixed; inset: 0; background: rgba(4, 6, 16, 0.7); display: grid; place-items: center; z-index: 20; }
  .dialog {
    width: 480px; max-height: 90vh; overflow: auto; background: var(--bg); border: 1px solid var(--line);
    border-radius: 12px; padding: 22px 24px; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);
  }
  .dialog.center { text-align: center; width: 440px; }
  .dialog h2 { margin: 0 0 12px; font-size: 19px; }
  label { display: block; color: var(--muted); font-size: 13px; margin: 10px 0 0; }
  input, select {
    display: block; width: 100%; margin-top: 4px; padding: 7px 9px; font: inherit; color: var(--text);
    background: var(--panel); border: 1px solid var(--line); border-radius: 6px; outline: none; user-select: text;
  }
  input:focus, select:focus { border-color: var(--green); }
  input:disabled { color: var(--muted); }
  .error { color: var(--red); font-size: 13px; margin: 12px 0 0; }
  .code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 38px; font-weight: 700; color: var(--green); letter-spacing: 3px; margin: 14px 0; user-select: text; }
  .small { font-size: 12px; }
  .row { display: flex; gap: 8px; justify-content: center; margin-top: 18px; }
  .row.end { justify-content: flex-end; }
</style>
