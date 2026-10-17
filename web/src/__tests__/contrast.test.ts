/// <reference types="vite/client" />
import css from "../styles.css?raw";
import mapCss from "../map/map.css?raw";

// The one violet palette in styles.css keeps WCAG AA contrast (NFR-08); aliases resolve one level.
const root = /^:root \{([^}]*)\}/m.exec(css)![1];
const raw = Object.fromEntries([...root.matchAll(/--([a-z0-9-]+):\s*([^;]+);/gi)].map((m) => [m[1], m[2].trim()]));
const t: Record<string, string> = Object.fromEntries(
  Object.entries(raw).map(([k, v]) => [k, v.startsWith("var(--") ? raw[v.slice(6, -1)] : v]),
);

const lum = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a: string, b: string) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};
const SURFACES = ["bg", "panel", "card"];

describe("Robin's palette", () => {
  test.each(["ink", "muted", "faint", "ok", "warn", "bad", "accent", "m-wait"])("%s text is at least 4.5:1", (k) => {
    for (const s of SURFACES) expect(ratio(t[k], t[s])).toBeGreaterThanOrEqual(4.5);
  });
  test("primary buttons are at least 4.5:1", () => expect(ratio(t["accent-ink"], t.accent)).toBeGreaterThanOrEqual(4.5));
  test.each(["focus", "field"])("%s is at least 3:1 against every surface", (k) => {
    for (const s of SURFACES) expect(ratio(t[k], t[s])).toBeGreaterThanOrEqual(3);
  });
  test("the Map defines no colours of its own", () => expect(mapCss).not.toMatch(/--m-[a-z-]+\s*:/));
});
