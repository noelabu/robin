import { useCallback, useSyncExternalStore } from "react";

export const PHONE_QUERY = "(max-width: 760px)";

/** Whether a media query matches now, following changes; false where matchMedia is missing (tests, old browsers). */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const m = window.matchMedia?.(query);
      m?.addEventListener("change", onChange);
      return () => m?.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () => window.matchMedia?.(query)?.matches ?? false);
}

export const useIsPhone = () => useMediaQuery(PHONE_QUERY);
