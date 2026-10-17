import type { CSSProperties } from "react";

// Violet is last: it is close to the background, so a folder gets it only when every other hue is taken.
const HUES = [
  "--h-cyan",
  "--h-green",
  "--h-blue",
  "--h-amber",
  "--h-pink",
  "--h-lime",
  "--h-peach",
  "--h-yellow",
  "--h-violet",
];

const hash = (key: string) => {
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % (HUES.length - 1);
};

/** The hue each folder was last given; a folder keeps it for as long as no other shown folder holds it. */
const given = new Map<string, number>();

/**
 * Gives the folders on screen distinct hues, in priority order (working folders first), so no two share a
 * colour until there are more folders than hues. A folder keeps the hue it had, else takes its own hash's hue,
 * else the first one free.
 */
export function assignHues(keys: string[]): void {
  const taken = new Set<number>();
  const out = new Map<string, number>();
  const unique = [...new Set(keys)];
  for (const k of unique) {
    const h = given.get(k);
    if (h !== undefined && !taken.has(h)) {
      out.set(k, h);
      taken.add(h);
    }
  }
  for (const k of unique) {
    if (out.has(k)) continue;
    const own = hash(k);
    const free = HUES.findIndex((_, i) => !taken.has(i));
    const h = !taken.has(own) ? own : free >= 0 ? free : own;
    out.set(k, h);
    taken.add(h);
  }
  for (const [k, h] of out) given.set(k, h);
}

/** A folder's colour: the same folder has the same colour on the Map and in Sessions. */
export function hue(key: string): CSSProperties {
  return { "--hue": `var(${HUES[given.get(key) ?? hash(key)]})` } as CSSProperties;
}
