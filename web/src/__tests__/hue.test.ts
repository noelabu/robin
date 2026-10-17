import { assignHues, hue } from "../map/hue";

const colour = (k: string) => (hue(k) as Record<string, string>)["--hue"];

describe("folder hues", () => {
  test("folders shown together never share a hue while hues remain", () => {
    const keys = Array.from({ length: 9 }, (_, i) => `/Users/x/project-${i}`);
    assignHues(keys);
    expect(new Set(keys.map(colour)).size).toBe(9);
  });
  test("a folder keeps its hue when others come and go", () => {
    assignHues(["/a/robin", "/a/wing-verifyiq"]);
    const before = colour("/a/robin");
    assignHues(["/b/other", "/a/robin"]);
    expect(colour("/a/robin")).toBe(before);
    expect(colour("/b/other")).not.toBe(before);
  });
});

test("violet goes only to a folder that finds every other hue taken", () => {
  const keys = Array.from({ length: 8 }, (_, i) => `/v/folder-${i}`);
  assignHues(keys);
  expect(keys.map(colour)).not.toContain("var(--h-violet)");
  assignHues([...keys, "/v/ninth"]);
  expect(colour("/v/ninth")).toBe("var(--h-violet)");
});
