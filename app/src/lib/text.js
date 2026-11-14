// Opis z Modrinth (Markdown) albo CurseForge (HTML) jako zwykły tekst.
export function plainText(raw) {
  let t = String(raw || "");
  if (/<\/?[a-z][\s\S]*?>/i.test(t)) {
    t = t
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/(p|div|h\d|li|tr|center)>/gi, "\n")
      .replace(/<li[^>]*>/gi, "• ");
    t = new DOMParser().parseFromString(t, "text/html").body.textContent || "";
  }
  return t
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s*/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "• ")
    .replace(/(\*\*|__|`|~~)/g, "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function formatCount(n) {
  for (const [limit, suffix] of [[1e6, " mln"], [1e3, " tys."]]) {
    if (n >= limit) return (n / limit).toFixed(1).replace(/\.0$/, "").replace(".", ",") + suffix;
  }
  return String(n);
}

export function pluralMods(n) {
  if (n === 1) return "1 mod";
  if ([2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100)) return `${n} mody`;
  return `${n} modów`;
}
