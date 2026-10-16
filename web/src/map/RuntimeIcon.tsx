/** A small mark per runtime; decorative, the runtime's name is always shown beside it. */
export function RuntimeIcon({ runtime }: { runtime: string }) {
  if (runtime === "claude")
    return (
      <svg className="map-rt" viewBox="0 0 24 24" aria-hidden="true">
        <g stroke="#d97757" strokeWidth="2.2" strokeLinecap="round">
          {[0, 30, 60, 90, 120, 150].map((a) => (
            <line key={a} x1="12" y1="3" x2="12" y2="21" transform={`rotate(${a} 12 12)`} />
          ))}
        </g>
      </svg>
    );
  if (runtime === "codex")
    return (
      <svg className="map-rt" viewBox="0 0 24 24" fill="none" stroke="#d6d4cd" strokeWidth="1.8" aria-hidden="true">
        <circle cx="12" cy="12" r="8.5" />
        <path d="M8.5 10l2.5 2-2.5 2M13 14.5h3" strokeLinecap="round" />
      </svg>
    );
  return (
    <svg className="map-rt" viewBox="0 0 24 24" fill="none" stroke="#9a7cf0" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M5 8h14M9 8v10M15 8v8.5c0 1 .6 1.5 1.5 1.5" />
    </svg>
  );
}
