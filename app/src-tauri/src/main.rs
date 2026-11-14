// Bez okna konsoli w wersji wydanej na Windowsie.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    zorza_lib::run()
}
