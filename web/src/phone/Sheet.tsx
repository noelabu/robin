import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * A panel that rises from the bottom over a scrim. Escape or a tap on the scrim closes it; focus goes back to what opened it.
 * `full` covers the whole screen, with a Close button. Over another sheet, Escape closes only the top one.
 */
export function Sheet(props: { label: string; onClose: () => void; full?: boolean; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const onClose = useRef(props.onClose);
  onClose.current = props.onClose;
  // Read while rendering: a child's effect (LaunchForm's) moves focus before this one runs.
  const [opener] = useState(() => document.activeElement as HTMLElement | null);
  useEffect(() => {
    if (!box.current?.contains(document.activeElement)) box.current?.querySelector<HTMLElement>("input, textarea, select, button")?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const sheets = document.querySelectorAll(".ph-sheet");
      if (sheets[sheets.length - 1] === box.current) onClose.current();
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      requestAnimationFrame(() => opener?.focus());
    };
  }, [opener]);
  return (
    <div className="ph-sheet-wrap">
      <div className="ph-scrim" onClick={props.onClose} aria-hidden="true" />
      <div className={props.full ? "ph-sheet full" : "ph-sheet"} role="dialog" aria-modal="true" aria-label={props.label} ref={box}>
        {props.full ? (
          <div className="ph-sheet-head">
            <span title={props.label}>{props.label}</span>
            <button type="button" onClick={props.onClose}>
              Close
            </button>
          </div>
        ) : (
          <span className="ph-grab" aria-hidden="true" />
        )}
        {props.children}
      </div>
    </div>
  );
}
