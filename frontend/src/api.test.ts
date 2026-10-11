import { afterEach, describe, it, expect, vi } from "vitest";
import { api, networkFailureEvent } from "./api";
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
describe("API boundary", () => {
  it("uses same-origin semantic endpoints with explicit mutation protection", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response('{"accepted":true}', { status: 200 }));
    expect(await api("/mower/dock", "POST", { confirmed: true })).toEqual({
      accepted: true,
    });
    expect(fetch.mock.calls[0][0]).toBe("/api/mower/dock");
    expect(fetch.mock.calls[0][1]?.headers).toHaveProperty(
      "X-Console-Request",
      "1",
    );
    expect(JSON.stringify(fetch.mock.calls)).not.toContain("Authorization");
  });
  it("surfaces unsupported capabilities rather than reporting success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response('{"detail":"Movement unsupported"}', { status: 409 }),
    );
    await expect(api("/movement/forward", "POST", {})).rejects.toThrow(
      "Movement unsupported",
    );
  });
  it("announces an unreachable console so the UI can recover on user activity", async () => {
    const browser = new EventTarget();
    class BrowserCustomEvent extends Event {
      detail: unknown;
      constructor(type: string, init?: CustomEventInit) {
        super(type);
        this.detail = init?.detail;
      }
    }
    vi.stubGlobal("window", browser);
    vi.stubGlobal("CustomEvent", BrowserCustomEvent);
    const listener = vi.fn();
    browser.addEventListener(networkFailureEvent, listener, { once: true });
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(api("/state")).rejects.toThrow("Failed to fetch");
    expect(listener).toHaveBeenCalledOnce();
  });
});
