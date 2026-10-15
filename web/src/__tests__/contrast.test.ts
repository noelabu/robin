/// <reference types="vite/client" />
import css from "../styles.css?raw";

// The colour tokens in styles.css keep WCAG AA contrast in both themes (NFR-08).
const block = (re: RegExp) => Object.fromEntries([...re.exec(css)![1].matchAll(/--([a-z-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
const light = block(/^:root \{([^}]*)\}/m);
const dark = { ...light, ...block(/prefers-color-scheme: dark\) \{\s*:root \{([^}]*)\}/) };

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

describe.each([["light", light], ["dark", dark]])("%s theme", (_, t) => {
  test.each(["ink", "muted", "ok", "warn", "bad"])("%s text is at least 4.5:1", (k) => {
    expect(ratio(t[k], t.bg)).toBeGreaterThanOrEqual(4.5);
    expect(ratio(t[k], t.panel)).toBeGreaterThanOrEqual(4.5);
  });
  test("primary buttons are at least 4.5:1", () => expect(ratio(t["accent-ink"], t.accent)).toBeGreaterThanOrEqual(4.5));
  test.each(["focus", "field"])("%s is at least 3:1 against the page", (k) => {
    expect(ratio(t[k], t.bg)).toBeGreaterThanOrEqual(3);
    expect(ratio(t[k], t.panel)).toBeGreaterThanOrEqual(3);
  });
});
