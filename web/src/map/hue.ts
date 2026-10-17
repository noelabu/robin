import type { CSSProperties } from "react";

const HUES = ["--h-violet", "--h-cyan", "--h-green", "--h-blue", "--h-amber", "--h-pink"];

/** A folder's colour, from its path: the same folder has the same colour on the Map and in Sessions. */
export function hue(key: string): CSSProperties {
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return { "--hue": `var(${HUES[h % HUES.length]})` } as CSSProperties;
}
