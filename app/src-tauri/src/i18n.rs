//! Jezyk komunikatow: angielski (domyslnie) albo polski.

use std::sync::atomic::{AtomicBool, Ordering};

static POLISH: AtomicBool = AtomicBool::new(false);

pub fn set_language(lang: &str) {
    POLISH.store(lang == "pl", Ordering::Relaxed);
}

pub fn is_pl() -> bool {
    POLISH.load(Ordering::Relaxed)
}

/// tr!("English {x}", "Polski {x}") -> String w biezacym jezyku.
#[macro_export]
macro_rules! tr {
    ($en:literal, $pl:literal $(, $arg:expr)* $(,)?) => {
        if $crate::i18n::is_pl() { format!($pl $(, $arg)*) } else { format!($en $(, $arg)*) }
    };
}
