# Zorza Client

Zorza Client is a hobby launcher for **Minecraft: Java Edition** that installs a mod loader (Fabric, Quilt, Forge or NeoForge) or plain Vanilla and starts the game.

*Polska wersja opisu znajduje się niżej.*

## What it does

- Downloads the game, libraries, assets and the required Java runtime **directly from Mojang's official servers** and verifies checksums.
- Mod loaders: Fabric (`meta.fabricmc.net`), Quilt (`meta.quiltmc.org`), Forge and NeoForge (official installers run headless with `--installClient`, first launch takes a few minutes), or Vanilla.
- Signs in **only with a Microsoft account** (OAuth 2.0 device code flow → Xbox Live → XSTS → Minecraft Services).
- Keeps its files in a separate folder (`.zorza`), so it does not touch the official launcher's installation.
- Prism-style layout: profile tiles, side panel with actions, profile editor (version, mods, per-profile RAM and JVM arguments) and a separate mod browser.
- Profiles: each profile has its own game folder (mods, worlds, options), game version, mod loader and optional loader version pin. Mods can be enabled or disabled without deleting them.
- Browses and installs **modpacks** (Modrinth `.mrpack` and CurseForge packs for any supported loader, also from a local file), **resource packs** and **data packs** (into a chosen world), with a details panel showing the full description, links, license and categories.
- Installs mods matching the profile's loader (Quilt profiles also accept Fabric mods) from **Modrinth** and **CurseForge** (CurseForge requires the user's own API key), including required dependencies. Mods whose authors disallow third-party distribution are not downloaded.

## What it does not do

- No offline, cracked or non-premium accounts. An account must own Minecraft: Java Edition.
- No redistribution of Minecraft files. Nothing from the game is bundled in this repository.
- No cheats or features that give an unfair advantage on servers.

## Account data

- The launcher never sees your Microsoft password: sign-in happens on Microsoft's website.
- Only a Microsoft refresh token is stored locally (`auth.json` in the launcher folder) so you stay signed in. It is not sent anywhere except Microsoft's sign-in endpoint.

## Running

Requires Python 3.9+ (standard library only).

```
python zorza_launcher.py          # window
python zorza_launcher.py --cli    # terminal
```

Supported game versions: 1.19 and newer.

## Status

Early development (version 0.1). Not affiliated with or endorsed by Mojang or Microsoft. Minecraft is a trademark of Mojang AB.

---

## Po polsku

Zorza Client to hobbystyczny launcher do **Minecraft: Java Edition**, który instaluje loader modów (Fabric, Quilt, Forge, NeoForge) albo czystą wersję Vanilla i uruchamia grę.

- Pobiera grę, biblioteki, zasoby i Javę **bezpośrednio z oficjalnych serwerów Mojangu** i sprawdza sumy kontrolne.
- Loguje **wyłącznie kontem Microsoft**; nie obsługuje kont nonpremium ani trybu offline.
- Nie rozpowszechnia plików gry i nie zawiera cheatów.
- Profile z osobnymi folderami gry oraz instalowanie modów pasujących do loadera profilu z Modrinth i CurseForge (CurseForge wymaga własnego klucza API).
- Lokalnie zapisuje tylko token odświeżania Microsoft (`auth.json`), żeby nie trzeba było logować się za każdym razem.

Modpacki (Modrinth i CurseForge), paczki zasobów i paczki danych, podgląd pełnego opisu i linków.

Uruchamianie: `python zorza_launcher.py` (wymaga Pythona 3.9+).

Projekt nie jest powiązany z Mojang ani Microsoft.
