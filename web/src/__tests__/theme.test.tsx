/// <reference types="vite/client" />
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { vi } from "vitest";
import themeScript from "../../public/theme.js?raw";
import { SettingsPanel } from "../components/SettingsPanel";
import { resolveTheme, savedTheme, type ThemeChoice, useTheme } from "../theme";

vi.mock("../api", async (orig) => {
  const m = await orig<typeof import("../api")>();
  return {
    ...m,
    api: {
      ...m.api,
      storage: () => new Promise(() => undefined),
      watch: () => new Promise(() => undefined),
      notifications: () => new Promise(() => undefined),
      remote: () => new Promise(() => undefined),
      questionWait: () => new Promise(() => undefined),
    },
  };
});

let osLight = false;
let listeners: (() => void)[] = [];
const setOs = (light: boolean) => {
  osLight = light;
  act(() => listeners.forEach((l) => l()));
};

beforeEach(() => {
  osLight = false;
  listeners = [];
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    get matches() {
      return query === "(prefers-color-scheme: light)" && osLight;
    },
    addEventListener: (_: string, l: () => void) => listeners.push(l),
    removeEventListener: (_: string, l: () => void) => (listeners = listeners.filter((x) => x !== l)),
  }));
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const applied = () => document.documentElement.dataset.theme;

describe("theme choice", () => {
  test("with nothing stored the system appearance decides", () => {
    expect(savedTheme()).toBe("system");
    expect(resolveTheme("system")).toBe("dark");
    osLight = true;
    expect(resolveTheme("system")).toBe("light");
  });
  test("a stored choice beats the system appearance", () => {
    osLight = false;
    localStorage.setItem("robin.theme", "light");
    renderHook(() => useTheme());
    expect(applied()).toBe("light");
    osLight = true;
    localStorage.setItem("robin.theme", "dark");
    renderHook(() => useTheme());
    expect(applied()).toBe("dark");
  });
  test("an unknown stored value means System", () => {
    localStorage.setItem("robin.theme", "purple");
    expect(savedTheme()).toBe("system");
  });
  test("storage that throws still applies a theme", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    osLight = true;
    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("system");
    expect(applied()).toBe("light");
    act(() => result.current[1]("dark"));
    expect(applied()).toBe("dark");
  });
  test("without matchMedia it is dark", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(resolveTheme("system")).toBe("dark");
    renderHook(() => useTheme());
    expect(applied()).toBe("dark");
  });
  test("System follows the appearance as it changes", () => {
    renderHook(() => useTheme());
    expect(applied()).toBe("dark");
    setOs(true);
    expect(applied()).toBe("light");
    setOs(false);
    expect(applied()).toBe("dark");
  });
  test("an explicit choice ignores appearance changes", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current[1]("dark"));
    setOs(true);
    expect(applied()).toBe("dark");
    expect(localStorage.getItem("robin.theme")).toBe("dark");
  });
});

describe("the pre-paint script", () => {
  const run = () => new Function(themeScript)();
  test.each([null, "light", "dark", "system", "purple"])("matches resolveTheme for stored %s", (stored) => {
    for (const os of [false, true]) {
      localStorage.clear();
      if (stored !== null) localStorage.setItem("robin.theme", stored);
      osLight = os;
      run();
      expect(applied()).toBe(resolveTheme(savedTheme()));
    }
  });
  test("survives storage that throws and no matchMedia", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.stubGlobal("matchMedia", undefined);
    run();
    expect(applied()).toBe("dark");
  });
});

function Harness() {
  const [theme, setTheme] = useTheme();
  return <SettingsPanel theme={theme} onTheme={setTheme} onClose={() => undefined} onChanged={() => undefined} />;
}

test("Settings offers System, Light and Dark, System by default", () => {
  render(<Harness />);
  const radio = (name: string) => screen.getByRole("radio", { name }) as HTMLInputElement;
  expect(screen.getByRole("group", { name: "Appearance" })).toBeTruthy();
  expect(radio("System").checked).toBe(true);
  fireEvent.click(radio("Light"));
  expect(radio("Light").checked).toBe(true);
  expect(applied()).toBe("light");
  expect(localStorage.getItem("robin.theme")).toBe("light");
  fireEvent.click(radio("System"));
  expect(applied()).toBe("dark");
  expect(savedTheme()).toBe<ThemeChoice>("system");
});
