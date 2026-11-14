<script>
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import { onMount } from "svelte";

  let info = $state(null);
  let latest = $state("sprawdzam…");
  let status = $state({ text: "Uruchamianie…", error: false });

  // logowanie
  let account = $state(null);
  let loggingIn = $state(false);
  let loginCode = $state(null); // { code, uri } gdy trzeba wpisać kod
  let accountMenu = $state(false);

  function setStatus(text, error = false) {
    status = { text, error };
  }

  async function login() {
    if (loggingIn) return;
    accountMenu = false;
    loggingIn = true;
    setStatus("Logowanie…");
    try {
      account = await invoke("login");
      setStatus(`Zalogowano jako ${account.name}`);
    } catch (e) {
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
    accountMenu = false;
    await invoke("logout");
    account = null;
    setStatus("Wylogowano");
  }

  function copyCode() {
    navigator.clipboard?.writeText(loginCode.code).catch(() => {});
  }

  onMount(() => {
    const unlisten = listen("login-code", (event) => {
      loginCode = event.payload;
      copyCode();
    });
    (async () => {
      try {
        info = await invoke("app_info");
        setStatus(`Zorza Client ${info.version} · dane: ${info.data_dir}`);
        if (await invoke("has_saved_login")) login(); // ciche odświeżenie zapisanej sesji
        latest = await invoke("latest_minecraft");
      } catch (e) {
        setStatus(String(e), true);
        latest = "nie udało się sprawdzić";
      }
    })();
    return () => unlisten.then((fn) => fn());
  });
</script>

<div class="window">
  <header class="banner">
    <div class="aurora a1"></div>
    <div class="aurora a2"></div>
    <div class="aurora a3"></div>
    <div class="logo">ZORZA <span>CLIENT</span></div>
  </header>

  <nav class="toolbar">
    <button disabled>＋ Dodaj profil</button>
    <button disabled>Modpacki…</button>
    <button disabled>Ustawienia</button>
    <div class="spacer"></div>
    <div class="menu-wrap">
      <button onclick={() => (account ? (accountMenu = !accountMenu) : login())} disabled={loggingIn && !loginCode}>
        {#if loggingIn}Logowanie…{:else if account}● {account.name} ▾{:else}Zaloguj się{/if}
      </button>
      {#if accountMenu && account}
        <div class="menu">
          <div class="menu-item muted">Zalogowano: {account.name}</div>
          <button class="menu-item" onclick={logout}>Wyloguj</button>
        </div>
      {/if}
    </div>
  </nav>

  <main class="body">
    <section class="tiles">
      <div class="tile selected">
        <div class="icon">D</div>
        <div class="name">Domyślny</div>
        <div class="sub">Minecraft {latest}</div>
      </div>
    </section>
    <aside class="side">
      <div class="big-icon">D</div>
      <div class="side-name">Domyślny</div>
      <div class="muted">Nowa wersja w budowie</div>
      <button class="primary wide" disabled>▶ Graj</button>
    </aside>
  </main>

  <footer class="status" class:err={status.error}>{status.text}</footer>
</div>

{#if loginCode}
  <div class="overlay">
    <div class="dialog">
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

<style>
  .window { display: flex; flex-direction: column; height: 100%; }
  .banner {
    position: relative; height: 64px; overflow: hidden; flex: none;
    background: linear-gradient(180deg, #04060f, var(--bg));
  }
  .aurora {
    position: absolute; left: -10%; width: 120%; height: 36px; border-radius: 50%;
    filter: blur(14px); opacity: 0.75; animation: drift 14s ease-in-out infinite alternate;
  }
  .a1 { top: 30px; background: linear-gradient(90deg, transparent, #3ddc97, #2bc4c9, transparent); }
  .a2 { top: 18px; background: linear-gradient(90deg, #2bc4c9, transparent, #3ddc97, transparent);
        animation-duration: 18s; opacity: 0.5; }
  .a3 { top: 6px; background: linear-gradient(90deg, transparent, #8b5cf6, transparent, #8b5cf6);
        animation-duration: 22s; opacity: 0.45; }
  @keyframes drift { from { transform: translateX(-4%) skewX(-8deg); } to { transform: translateX(4%) skewX(8deg); } }
  .logo {
    position: relative; padding: 14px 24px; font-size: 28px; font-weight: 800; letter-spacing: 1px;
    text-shadow: 0 0 18px rgba(61, 220, 151, 0.45);
  }
  .logo span { font-size: 12px; font-weight: 400; color: #b8f5da; letter-spacing: 2px; }
  .toolbar { display: flex; gap: 6px; padding: 7px 10px; background: var(--panel); flex: none; }
  .spacer { flex: 1; }
  .body { flex: 1; display: flex; min-height: 0; }
  .tiles { flex: 1; padding: 16px; display: flex; flex-wrap: wrap; align-content: flex-start; gap: 8px; overflow: auto; }
  .tile { width: 128px; padding: 12px 6px; border-radius: 12px; text-align: center; cursor: default; }
  .tile.selected { background: var(--select); }
  .icon, .big-icon {
    margin: 0 auto; display: grid; place-items: center; font-weight: 800; color: var(--bg);
    background: #d16ba5; border-radius: 16px;
  }
  .icon { width: 64px; height: 64px; font-size: 30px; }
  .big-icon { width: 76px; height: 76px; font-size: 36px; margin-top: 22px; border-radius: 18px; }
  .name { margin-top: 10px; font-weight: 700; }
  .sub, .muted { color: var(--muted); font-size: 12px; }
  .side { width: 250px; background: var(--panel); display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 0 18px; flex: none; }
  .side-name { font-size: 17px; font-weight: 700; margin-top: 6px; }
  .wide { width: 100%; margin-top: 12px; padding: 10px; }
  .status { background: var(--panel); color: var(--muted); font-size: 12px; padding: 6px 12px; flex: none;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; user-select: text; }
  .status.err { color: var(--red); }
  .menu-wrap { position: relative; }
  .menu {
    position: absolute; right: 0; top: calc(100% + 4px); min-width: 200px; z-index: 10;
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 4px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }
  .menu-item { display: block; width: 100%; text-align: left; padding: 8px 10px; background: none; border-radius: 6px; }
  .overlay { position: fixed; inset: 0; background: rgba(4, 6, 16, 0.7); display: grid; place-items: center; z-index: 20; }
  .dialog {
    width: 440px; background: var(--bg); border: 1px solid var(--line); border-radius: 12px;
    padding: 24px; text-align: center; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);
  }
  .dialog h2 { margin: 0 0 6px; font-size: 19px; }
  .code {
    font-family: Consolas, "Cascadia Mono", monospace; font-size: 38px; font-weight: 700;
    color: var(--green); letter-spacing: 3px; margin: 14px 0; user-select: text;
  }
  .small { font-size: 12px; }
  .row { display: flex; gap: 8px; justify-content: center; margin-top: 18px; }
</style>
