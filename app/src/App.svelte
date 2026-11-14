<script>
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";

  let info = $state(null);
  let latest = $state("sprawdzam…");
  let error = $state("");

  onMount(async () => {
    try {
      info = await invoke("app_info");
      latest = await invoke("latest_minecraft");
    } catch (e) {
      error = String(e);
      latest = "nie udało się sprawdzić";
    }
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
    <button disabled>Zaloguj się ▾</button>
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

  <footer class="status">
    {#if error}
      <span class="err">Błąd: {error}</span>
    {:else if info}
      Zorza Client {info.version} (Tauri) · {info.os} · dane: {info.data_dir}
    {:else}
      Uruchamianie…
    {/if}
  </footer>
</div>

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
  .err { color: var(--red); }
</style>
