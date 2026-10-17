import { useState } from "react";
import type { MapState } from "./api";

/** How far back the Map and the session list look: what's going on now, today, or everything. */
export type Recency = "active" | "today" | "all";

const KEY = "robin.recency";
const HOUR = 3_600_000;
const TODAY_MS = 24 * HOUR;

/**
 * Active is only what's working now or waiting on you. Today adds anything that ran in the last day;
 * working and waiting sessions are in every view, whatever their age.
 */
export function inRecency(state: MapState, lastActivity: string | null, now: number, r: Recency): boolean {
  if (r === "all" || state === "running" || state === "waiting_for_input") return true;
  if (r === "active") return false;
  const age = lastActivity ? now - Date.parse(lastActivity) : Infinity;
  return age < TODAY_MS;
}

function saved(): Recency {
  try {
    const v = localStorage.getItem(KEY);
    return v === "today" || v === "all" ? v : "active";
  } catch {
    return "active";
  }
}

/** The choice is shared by the Map and the session list and remembered in this browser. */
export function useRecency(): [Recency, (r: Recency) => void] {
  const [r, setR] = useState<Recency>(saved);
  return [
    r,
    (next) => {
      setR(next);
      try {
        localStorage.setItem(KEY, next);
      } catch {
        /* not remembered */
      }
    },
  ];
}
