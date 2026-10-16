import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { Packet, Session } from "../api";
import { SharePanel } from "../components/FindingsCompare";

const calls: { allow_loops?: string[] }[] = [];

vi.mock("../api", async (orig) => {
  const m = await orig<typeof import("../api")>();
  const packet = (allow: string[]): Packet => ({
    packet_id: "p1",
    state: "previewed",
    invalid_reason: null,
    recipient: { session_id: "s1", runtime: "claude", independence: null },
    question: "q",
    sources: allow.length ? [{ n: 1, kind: "finding", id: "f1", label: "finding f1", content_hash: "h" }] : [],
    redactions: [],
    excluded: allow.length
      ? []
      : [{ kind: "finding", id: "f1", label: "finding f1", reason: "the recipient wrote this" }],
    flags: [],
    bytes: 10,
    approx_tokens: 3,
    payload_hash: "sha256:x",
    deliveries: [],
  });
  return {
    ...m,
    api: {
      ...m.api,
      previewContext: async (body: { allow_loops?: string[] }) => {
        calls.push(body);
        return packet(body.allow_loops ?? []);
      },
    },
  };
});

test("a source left out as the recipient's own output can be included on request", async () => {
  const session = { session_id: "s1", mode: "managed", archived: false, runtime: "claude", runtime_state: "idle" };
  render(
    <SharePanel
      taskId="t1"
      picks={[{ id: "f1", version: 1, claims: null }]}
      sessions={[session as unknown as Session]}
      onClose={() => {}}
    />,
  );
  fireEvent.change(screen.getByLabelText("What should it do with this?"), { target: { value: "Review" } });
  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await waitFor(() => expect(screen.getByText(/Left out: finding f1 \(the recipient wrote this\)/)).toBeTruthy());
  expect(calls[0].allow_loops).toEqual([]);

  fireEvent.click(screen.getByRole("button", { name: "Include anyway" }));
  await waitFor(() => expect(screen.queryByText(/Left out/)).toBeNull());
  expect(calls[1].allow_loops).toEqual(["f1"]);
});
