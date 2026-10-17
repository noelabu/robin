import type { Edge } from "./layout";
import { hue } from "./hue";

const PACKETS = 3;
const still = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** A session-to-subagent wire, in its folder's hue; packets run along it while the subagent works. */
export function Wire({ edge, transform, arrow }: { edge: Edge; transform?: string; arrow?: boolean }) {
  const id = `wire-${edge.to.replace(/[^A-Za-z0-9_-]/g, "_")}`;
  return (
    <g style={hue(edge.cluster)} transform={transform}>
      <path
        id={id}
        d={edge.d}
        className={edge.live ? "live" : ""}
        markerEnd={arrow ? `url(#${edge.live ? "map-arrow-live" : "map-arrow"})` : undefined}
      />
      {edge.live &&
        !still() &&
        Array.from({ length: PACKETS }, (_, i) => (
          <circle key={i} r="2.6" className="map-packet">
            <animateMotion dur="1.8s" begin={`${(i * 1.8) / PACKETS}s`} repeatCount="indefinite">
              <mpath href={`#${id}`} />
            </animateMotion>
          </circle>
        ))}
    </g>
  );
}
