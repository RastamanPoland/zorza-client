<script>
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { plainText, formatCount } from "./lib/text.js";

  // pid: profil docelowy; profile: jego dane; onModpack(hit): instalacja modpacka jako nowego profilu
  let { pid, profile, initialKind = "mod", onclose, onchanged, onModpack } = $props();

  const KINDS = [
    ["mod", "Mody"],
    ["modpack", "Modpacki"],
    ["resourcepack", "Paczki zasobów"],
    ["datapack", "Paczki danych"],
  ];
  const SORTS = [
    ["downloads", "Popularność"],
    ["relevance", "Trafność"],
    ["updated", "Ostatnio zaktualizowane"],
    ["newest", "Najnowsze"],
  ];
  const LOADER_NAME = { fabric: "Fabric", quilt: "Quilt", forge: "Forge", neoforge: "NeoForge", vanilla: "Vanilla" };

  let kind = $state(initialKind);
  let source = $state("modrinth");
  let query = $state("");
  let sort = $state("downloads");
  let results = $state([]);
  let version = $state("");
  let end = $state(false);
  let loading = $state(false);
  let info = $state({ text: "", error: false });
  let installed = $state(new Set());
  let current = $state(null); // zaznaczony wynik
  let details = $state(null);
  let detailsError = $state("");
  let message = $state({ text: "", error: false });
  let installing = $state(false);
  let worlds = $state([]);
  let world = $state("");
  let cfKey = $state(true);
  let req = 0;
  let detailReq = 0;
  const detailsCache = new Map();

  async function refreshInstalled() {
    try {
      installed = new Set(await invoke("installed_keys", { pid, kind }));
    } catch {
      installed = new Set();
    }
  }

  async function load(more = false) {
    if (source === "curseforge" && !cfKey) {
      results = [];
      info = { text: "Brak klucza API CurseForge. Wpisz własny w Ustawieniach albo użyj Modrinth.", error: true };
      return;
    }
    if (kind === "mod" && profile.loader === "vanilla") {
      results = [];
      info = { text: "Ten profil nie ma loadera modów. Wybierz Fabric, Quilt, Forge albo NeoForge w edycji profilu.", error: true };
      return;
    }
    if (more && (end || loading)) return;
    const my = ++req;
    loading = true;
    info = { text: more ? "Wczytuję kolejne…" : query.trim() ? "Szukam…" : "Wczytuję najpopularniejsze…", error: false };
    try {
      const [v, hits] = await invoke("search_content", {
        kind, source, query, pid, sort: source === "modrinth" ? sort : "", offset: more ? results.length : 0,
      });
      if (my !== req) return;
      version = v;
      if (more) {
        const seen = new Set(results.map((r) => r.source + ":" + r.id));
        results = [...results, ...hits.filter((h) => !seen.has(h.source + ":" + h.id))];
      } else {
        results = hits;
        current = null;
      }
      end = hits.length < 30;
      const where = source === "modrinth" ? "Modrinth" : "CurseForge";
      const forVersion = kind === "modpack" ? "" : ` dla Minecraft ${v}${kind === "mod" ? " · " + LOADER_NAME[profile.loader] : ""}`;
      info = { text: results.length ? `${where}: ${results.length} wyników${forVersion}` : `${where}: brak wyników${forVersion}`, error: false };
    } catch (e) {
      if (my === req) info = { text: String(e), error: true };
    } finally {
      if (my === req) loading = false;
    }
  }

  async function show(r) {
    current = r;
    message = { text: "", error: false };
    detailsError = "";
    const key = r.source + ":" + r.id;
    if (detailsCache.has(key)) return (details = detailsCache.get(key));
    details = null;
    const my = ++detailReq;
    try {
      const d = await invoke("project_details", { hit: r });
      detailsCache.set(key, d);
      if (my === detailReq) details = d;
    } catch (e) {
      if (my === detailReq) detailsError = String(e);
    }
  }

  async function install() {
    const r = current;
    if (!r || installing) return;
    if (kind === "modpack") {
      message = { text: `Instaluję modpack „${r.title}”… Postęp widać na dole głównego okna.`, error: false };
      onModpack(r);
      return;
    }
    if (kind === "datapack" && !world) return (message = { text: "Wybierz świat.", error: true });
    installing = true;
    message = { text: `Instaluję: ${r.title}…`, error: false };
    try {
      const names = await invoke("install_content", { pid, kind, hit: r, world: kind === "datapack" ? world : null });
      message = { text: "Zainstalowano: " + names.join(", ") + (kind === "datapack" ? ` (świat: ${world})` : ""), error: false };
      await refreshInstalled();
      onchanged?.();
    } catch (e) {
      message = { text: String(e), error: true };
    } finally {
      installing = false;
    }
  }

  function setKind(k) {
    kind = k;
    current = null;
    refreshInstalled();
    load();
  }

  function setSource(src) {
    source = src;
    current = null;
    load();
  }

  onMount(async () => {
    cfKey = await invoke("has_curseforge_key").catch(() => false);
    worlds = await invoke("list_worlds", { pid }).catch(() => []);
    world = worlds[0] || "";
    refreshInstalled();
    load();
  });
</script>

<div class="overlay">
  <div class="panel">
    <div class="tabs">
      {#each KINDS as [k, label]}
        <button class:on={kind === k} onclick={() => setKind(k)}>{label}</button>
      {/each}
      <div class="spacer"></div>
      <button class:on={source === "modrinth"} onclick={() => setSource("modrinth")}>Modrinth</button>
      <button class:on={source === "curseforge"} onclick={() => setSource("curseforge")}>CurseForge</button>
    </div>
    <div class="search">
      <input bind:value={query} placeholder="Szukaj…" onkeydown={(e) => e.key === "Enter" && load()} />
      <select bind:value={sort} onchange={() => load()} disabled={source !== "modrinth"}>
        {#each SORTS as [id, label]}<option value={id}>{label}</option>{/each}
      </select>
      <button class="primary" onclick={() => load()}>Szukaj</button>
    </div>

    <div class="content">
      <div class="results" onscroll={(e) => {
        const el = e.currentTarget;
        if (el.scrollTop + el.clientHeight > el.scrollHeight - 80) load(true);
      }}>
        {#each results as r (r.source + r.id)}
          <button class="result" class:sel={current === r} onclick={() => show(r)} ondblclick={install}>
            {#if r.icon}<img src={r.icon} alt="" loading="lazy" />{:else}<div class="noicon">{r.title[0]}</div>{/if}
            <div class="rtext">
              <div class="rtitle">
                {#if installed.has(r.source + ":" + r.id)}<span class="ok">✓</span>{/if}
                {r.title}
                <span class="muted">· {formatCount(r.downloads)} pobrań</span>
              </div>
              <div class="muted clip">{r.description}</div>
            </div>
          </button>
        {/each}
        {#if !end && results.length}
          <button class="more" onclick={() => load(true)} disabled={loading}>Pokaż więcej</button>
        {/if}
      </div>

      <aside class="details">
        {#if current}
          <div class="head">
            {#if current.icon}<img src={current.icon} alt="" />{/if}
            <div>
              <div class="dtitle">{current.title}</div>
              {#if current.author}<div class="muted">autor: {current.author}</div>{/if}
            </div>
          </div>
          <div class="muted meta">
            {formatCount(current.downloads)} pobrań
            {#if details?.followers} · {formatCount(details.followers)} obserwujących{/if}
            {#if details?.updated} · aktualizacja: {details.updated}{/if}
            {#if details?.license} · licencja: {details.license}{/if}
            {#if details?.categories?.length}<br />Kategorie: {details.categories.slice(0, 8).join(", "){/if}
          </div>
          <div class="links">
            {#each details?.links ?? (current.url ? [["Strona projektu", current.url]] : []) as [label, url]}
              <button onclick={() => invoke("open_url", { url })}>{label}</button>
            {/each}
          </div>
          <div class="desc">
            {#if details}{plainText(details.description) || "Brak opisu."}
            {:else if detailsError}{current.description || "Nie udało się wczytać opisu."}
            {:else}{current.description}<br /><br /><span class="muted">Wczytuję pełny opis…</span>{/if}
          </div>
          {#if kind === "datapack"}
            {#if worlds.length}
              <label>Świat
                <select bind:value={world}>{#each worlds as w}<option value={w}>{w}</option>{/each}</select>
              </label>
            {:else}
              <p class="muted small">Ten profil nie ma jeszcze świata. Uruchom grę i utwórz świat.</p>
            {/if}
          {/if}
          {#if message.text}<p class="msg" class:err={message.error}>{message.text}</p>{/if}
          <button class="primary install" onclick={install} disabled={installing || (kind === "datapack" && !worlds.length)}>
            {#if installing}Instaluję…
            {:else if kind === "modpack"}Zainstaluj jako nowy profil
            {:else if kind === "datapack"}Dodaj do świata
            {:else if installed.has(current.source + ":" + current.id)}Zainstaluj ponownie / zaktualizuj
            {:else}Zainstaluj{/if}
          </button>
        {:else}
          <p class="muted">Wybierz pozycję z listy, żeby zobaczyć opis.</p>
        {/if}
      </aside>
    </div>

    <div class="bottom">
      <span class="info" class:err={info.error}>{info.text}</span>
      <span class="muted">
        {#if kind === "modpack"}Modpack zostanie zainstalowany jako nowy profil
        {:else if kind === "datapack"}Paczka danych trafi do wybranego świata
        {:else}Instalacja do profilu „{profile.name}”{/if}
      </span>
      <button onclick={onclose}>Zamknij</button>
    </div>
  </div>
</div>

<style>
  .overlay { position: fixed; inset: 0; background: rgba(4, 6, 16, 0.75); display: grid; place-items: center; z-index: 15; }
  .panel {
    width: min(1180px, 96vw); height: min(760px, 94vh); background: var(--bg); border: 1px solid var(--line);
    border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);
  }
  .tabs, .search, .bottom { display: flex; gap: 6px; padding: 8px 12px; background: var(--panel); align-items: center; }
  .tabs { padding-top: 10px; }
  .tabs .on { background: var(--select); }
  .spacer { flex: 1; }
  .search input { flex: 1; }
  .search select { width: 220px; }
  input, select {
    padding: 7px 9px; font: inherit; color: var(--text); background: var(--bg); border: 1px solid var(--line);
    border-radius: 6px; outline: none; user-select: text;
  }
  input:focus { border-color: var(--green); }
  .content { flex: 1; display: flex; min-height: 0; }
  .results { flex: 1; overflow: auto; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; }
  .result { display: flex; gap: 10px; align-items: center; text-align: left; background: none; padding: 8px; border-radius: 8px; }
  .result:hover { background: var(--panel2); }
  .result.sel { background: var(--select); }
  .result img, .noicon { width: 40px; height: 40px; border-radius: 8px; flex: none; object-fit: cover; }
  .noicon { display: grid; place-items: center; background: var(--panel2); font-weight: 700; }
  .rtext { min-width: 0; }
  .rtitle { font-weight: 600; }
  .ok { color: var(--green); margin-right: 4px; }
  .clip { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .more { margin: 8px auto; }
  .details { width: 400px; flex: none; background: var(--panel); padding: 16px; display: flex; flex-direction: column; gap: 8px; min-height: 0; }
  .head { display: flex; gap: 12px; align-items: center; }
  .head img { width: 64px; height: 64px; border-radius: 12px; object-fit: cover; }
  .dtitle { font-size: 17px; font-weight: 700; }
  .meta { font-size: 12px; line-height: 1.5; }
  .links { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
  .links button { padding: 6px 8px; font-size: 13px; }
  .desc { flex: 1; overflow: auto; white-space: pre-wrap; font-size: 13px; line-height: 1.5; user-select: text; padding-right: 4px; }
  .install { padding: 10px; }
  label { color: var(--muted); font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
  .msg { margin: 0; font-size: 13px; color: var(--green); }
  .msg.err, .info.err { color: var(--red); }
  .info { flex: 1; font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .muted { color: var(--muted); font-size: 12px; }
  .small { font-size: 12px; }
</style>
