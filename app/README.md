# Zorza Client (Tauri)

Nowa wersja launchera: logika w Ruście, okno w Svelte. W budowie; działająca wersja w Pythonie jest w `../zorza_launcher.py`.
Obie wersje używają tego samego folderu danych (`%APPDATA%\.zorza` na Windowsie).

## Gotowe pliki

Każde wysłanie zmian w `app/` buduje launcher na GitHubie: zakładka **Actions → Zorza Client (Tauri)** → ostatnie uruchomienie → **Artifacts**.

## Kompilacja u siebie (Windows)

1. Visual Studio Build Tools z „Desktop development with C++”.
2. Rust: https://rustup.rs
3. Node.js 20+.

```
cd app
npm install
npm run tauri dev     # okno z podglądem zmian na żywo
npm run tauri build   # instalator w src-tauri/target/release/bundle
```
