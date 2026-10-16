import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { Layout, Rect } from "./layout";

export type CanvasHandle = { panTo: (r: Rect) => void; fit: () => void; reveal: (r: Rect) => void };
type View = { x: number; y: number; k: number };

const MIN_K = 0.25;
const MAX_K = 2;
const MARGIN = 400;
const READABLE = 0.7; // world pixels kept rendered outside the viewport

/** Pan and zoom by hand: one transform on the world, arrows in SVG under the cards, viewport culling. */
export const Canvas = forwardRef<
  CanvasHandle,
  { layout: Layout; overlay: ReactNode; children: (visible: (r: Rect) => boolean) => ReactNode; onBackground: () => void }
>(function Canvas({ layout, overlay, children, onBackground }, ref) {
  const stage = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ x: 40, y: 80, k: 1 });
  const [easing, setEasing] = useState(false);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const fitted = useRef(false);

  useLayoutEffect(() => {
    const el = stage.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => ro?.disconnect();
  }, []);

  const ease = (v: View) => {
    setEasing(true);
    setView(v);
    window.setTimeout(() => setEasing(false), 320);
  };
  const zoomAt = useCallback((factor: number, cx: number, cy: number) => {
    setView((v) => {
      const nk = Math.min(MAX_K, Math.max(MIN_K, v.k * factor));
      return { k: nk, x: cx - (cx - v.x) * (nk / v.k), y: cy - (cy - v.y) * (nk / v.k) };
    });
  }, []);
  const fit = useCallback(() => {
    const { w, h } = layout.bounds;
    if (!w || !size.w) return;
    const k = Math.min(1, Math.max(MIN_K, Math.min((size.w - 80) / w, (size.h - 180) / h)));
    ease({ k, x: Math.max(40, (size.w - w * k) / 2), y: Math.max(100, (size.h - h * k) / 2) });
  }, [layout.bounds, size]);
  const panTo = useCallback(
    (r: Rect) => ease({ ...view, x: size.w / 2 - (r.x + r.w / 2) * view.k, y: size.h / 2 - (r.y + Math.min(r.h, 200) / 2) * view.k }),
    [view, size],
  );
  const reveal = useCallback(
    (r: Rect) => {
      const left = r.x * view.k + view.x;
      const top = r.y * view.k + view.y;
      if (left < 0 || top < 0 || left + r.w * view.k > size.w || top + r.h * view.k > size.h) panTo(r);
    },
    [view, size, panTo],
  );
  useImperativeHandle(ref, () => ({ panTo, fit, reveal }), [panTo, fit, reveal]);

  // First view: everything if it fits legibly, otherwise the top-left (most recent) clusters at READABLE.
  useEffect(() => {
    if (fitted.current || !layout.bounds.w || !size.w) return;
    fitted.current = true;
    const { w, h } = layout.bounds;
    const k = Math.min(1, Math.min((size.w - 80) / w, (size.h - 180) / h));
    if (k >= READABLE) fit();
    else setView({ k: READABLE, x: 40, y: 100 });
  }, [layout.bounds, size.w, size.h, fit]);

  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      if (e.metaKey || e.ctrlKey) zoomAt(Math.exp(-e.deltaY / 300), e.clientX - r.left, e.clientY - r.top);
      else setView((v) => ({ ...v, x: v.x - e.deltaX, y: v.y - e.deltaY }));
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [zoomAt]);

  const zoomBy = (f: number) => zoomAt(f, size.w / 2, size.h / 2);

  // With no measured size (tests, first paint) everything renders.
  const visible = (r: Rect) => {
    if (!size.w) return true;
    const x0 = -view.x / view.k - MARGIN;
    const y0 = -view.y / view.k - MARGIN;
    const x1 = (size.w - view.x) / view.k + MARGIN;
    const y1 = (size.h - view.y) / view.k + MARGIN;
    return r.x + r.w > x0 && r.x < x1 && r.y + r.h > y0 && r.y < y1;
  };

  return (
    <div
      ref={stage}
      className={`map-stage ${drag.current?.moved ? "dragging" : ""}`}
      aria-label="Session map"
      role="region"
      tabIndex={-1}
      onScroll={(e) => {
        // Focus would scroll this overflow-hidden box; the canvas pans instead (see reveal).
        e.currentTarget.scrollTop = 0;
        e.currentTarget.scrollLeft = 0;
      }}
      style={{ backgroundPosition: `${view.x}px ${view.y}px`, backgroundSize: `${22 * view.k}px ${22 * view.k}px` }}
      onPointerDown={(e) => {
        if ((e.target as HTMLElement).closest("button, a, input, .map-drawer")) return;
        drag.current = { x: e.clientX - view.x, y: e.clientY - view.y, moved: false };
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        d.moved = true;
        setView((v) => ({ ...v, x: e.clientX - d.x, y: e.clientY - d.y }));
      }}
      onPointerUp={(e) => {
        const d = drag.current;
        drag.current = null;
        if (d && !d.moved && !(e.target as HTMLElement).closest(".map-drawer")) onBackground();
      }}
      onKeyDown={(e) => {
        if ((e.target as HTMLElement).closest("input, textarea")) return;
        if (e.key === "+" || e.key === "=") zoomBy(1.2);
        else if (e.key === "-") zoomBy(1 / 1.2);
        else if (e.key === "0") fit();
      }}
    >
      <div className="map-zoom" role="group" aria-label="Zoom">
        <button aria-label="Zoom out" onClick={() => zoomBy(1 / 1.2)}>
          −
        </button>
        <output aria-live="polite">{Math.round(view.k * 100)}%</output>
        <button aria-label="Zoom in" onClick={() => zoomBy(1.2)}>
          +
        </button>
        <span className="sep" />
        <button aria-label="Fit to screen" onClick={fit}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <path d="M1 5V1h4M9 1h4v4M13 9v4H9M5 13H1V9" />
          </svg>
        </button>
      </div>
      <div
        className={`map-world ${easing ? "easing" : ""}`}
        data-testid="map-world"
        style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}
      >
        <svg className="map-wires" width={layout.bounds.w} height={layout.bounds.h} aria-hidden="true">
          <defs>
            <marker id="map-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0 0L8 4L0 8z" fill="#5f5378" stroke="none" />
            </marker>
            <marker id="map-arrow-live" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0 0L8 4L0 8z" fill="#b197fc" stroke="none" />
            </marker>
          </defs>
          {layout.edges.map((e) => (
            <path key={e.to} d={e.d} className={e.live ? "live" : ""} markerEnd={`url(#${e.live ? "map-arrow-live" : "map-arrow"})`} />
          ))}
        </svg>
        {children(visible)}
      </div>
      {overlay}
    </div>
  );
});
