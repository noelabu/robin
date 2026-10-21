/// <reference types="vite/client" />
import css from "../styles.css?raw";
import mapCss from "../map/map.css?raw";

// Robin's violet palette, dark and light, keeps WCAG AA contrast (NFR-08); aliases resolve one level.
const vars = (block: string) =>
  Object.fromEntries([...block.matchAll(/--([a-z0-9-]+):\s*([^;]+);/gi)].map((m) => [m[1], m[2].trim()]));
const dark = vars(/^:root \{([^}]*)\}/m.exec(css)![1]);
const light = { ...dark, ...vars(/^:root\[data-theme="light"\] \{([^}]*)\}/m.exec(css)![1]) };
const resolve = (raw: Record<string, string>): Record<string, string> =>
  Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, v.startsWith("var(--") ? raw[v.slice(6, -1)] : v]));
const PALETTES: [string, Record<string, string>][] = [
  ["dark", resolve(dark)],
  ["light", resolve(light)],
];

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

describe.each(PALETTES)("Robin's %s palette", (_, t) => {
  test.each(["ink", "muted", "faint", "ok", "warn", "bad", "accent", "m-wait"])("%s text is at least 4.5:1", (k) => {
    for (const s of SURFACES) expect(ratio(t[k], t[s])).toBeGreaterThanOrEqual(4.5);
  });
  test("primary buttons are at least 4.5:1", () => expect(ratio(t["accent-ink"], t.accent)).toBeGreaterThanOrEqual(4.5));
  test.each(["focus", "field"])("%s is at least 3:1 against every surface", (k) => {
    for (const s of SURFACES) expect(ratio(t[k], t[s])).toBeGreaterThanOrEqual(3);
  });
  test.each(Object.keys(t).filter((k) => k.startsWith("h-")))("folder hue %s is at least 4.5:1", (k) => {
    for (const s of SURFACES) expect(ratio(t[k], t[s])).toBeGreaterThanOrEqual(4.5);
  });
  test.each([
    ["m-warn-ink", "m-warn-bg"],
    ["m-need-ink", "m-warn-bg"],
    ["m-ok-ink", "m-ok-bg"],
    ["m-done-ink", "m-done-bg"],
    ["m-live-ink", "m-live-bg"],
    ["m-live-ink", "m-live-pill"],
  ])("%s on %s is at least 4.5:1", (ink, bg) => expect(ratio(t[ink], t[bg])).toBeGreaterThanOrEqual(4.5));
  test("checkboxes are at least 3:1", () => {
    for (const s of SURFACES) expect(ratio(t["m-box"], t[s])).toBeGreaterThanOrEqual(3);
  });
});
test("the Map defines no colours of its own", () => expect(mapCss).not.toMatch(/--m-[a-z-]+\s*:/));
