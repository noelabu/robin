import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { api, type RemoteSettings } from "../api";
import { SettingsPanel } from "../components/SettingsPanel";

const never = () => new Promise<never>(() => undefined);
const props = { theme: "system" as const, onTheme: () => undefined, onClose: () => undefined, onChanged: () => undefined };

function remote(value: RemoteSettings) {
  for (const k of ["storage", "watch", "notifications", "questionWait"] as const) vi.spyOn(api, k).mockImplementation(never);
  vi.spyOn(api, "remote").mockResolvedValue(value);
}

afterEach(() => vi.restoreAllMocks());

describe("remote access settings", () => {
  test("off: says how to turn it on", async () => {
    remote({ enabled: false, listening: false, urls: [], owner: null, problem: null });
    render(<SettingsPanel {...props} />);
    expect(await screen.findByText(/robin remote on/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in on phone" })).toBeNull();
  });

  test("on: shows the address and who can open it, with no sign-in link", async () => {
    remote({
      enabled: true,
      listening: true,
      urls: ["http://mac:7333", "http://mac.tail1.ts.net:7333"],
      owner: "me@example.com",
      problem: null,
    });
    render(<SettingsPanel {...props} />);
    expect(await screen.findByText("http://mac:7333")).toBeTruthy();
    expect(screen.getByText(/signed in to Tailscale as me@example.com/)).toBeTruthy();
    expect(screen.getByText(/Add to Home Screen/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in on phone" })).toBeNull();
  });

  test("on but Tailscale unreachable: says why", async () => {
    remote({ enabled: true, listening: false, urls: [], owner: null, problem: "Tailscale isn't connected" });
    render(<SettingsPanel {...props} />);
    expect(await screen.findByText(/waiting for Tailscale: Tailscale isn't connected/)).toBeTruthy();
  });
});
