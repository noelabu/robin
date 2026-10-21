import { useEffect, useState } from "react";

/** Light or dark, or whatever the system appearance is. public/theme.js applies it before the first paint. */
export type ThemeChoice = "system" | "light" | "dark";

const KEY = "robin.theme";
const QUERY = "(prefers-color-scheme: light)";

const media = () => (typeof window !== "undefined" ? window.matchMedia?.(QUERY) : undefined);

export function savedTheme(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function resolveTheme(choice: ThemeChoice): "light" | "dark" {
  if (choice !== "system") return choice;
  return media()?.matches ? "light" : "dark";
}

export function applyTheme(choice: ThemeChoice) {
  document.documentElement.dataset.theme = resolveTheme(choice);
}

/** Remembered in this browser; while it's System, follows the appearance as it changes. */
export function useTheme(): [ThemeChoice, (t: ThemeChoice) => void] {
  const [theme, setTheme] = useState<ThemeChoice>(savedTheme);
  useEffect(() => {
    applyTheme(theme);
    if (theme !== "system") return;
    const mq = media();
    if (!mq) return;
    const follow = () => applyTheme("system");
    mq.addEventListener?.("change", follow);
    return () => mq.removeEventListener?.("change", follow);
  }, [theme]);
  return [
    theme,
    (next) => {
      setTheme(next);
      try {
        if (next === "system") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, next);
      } catch {
        /* not remembered */
      }
      applyTheme(next);
    },
  ];
}
