// Język interfejsu: "en" (domyślnie) albo "pl". t("English", "Polski") zwraca tekst w bieżącym języku.
export const i18n = $state({ lang: "en" });

export function t(en, pl) {
  return i18n.lang === "pl" ? pl : en;
}
