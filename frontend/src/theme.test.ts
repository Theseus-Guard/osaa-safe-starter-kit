import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getPreference,
  initTheme,
  isThemePreference,
  setPreference,
  useResolvedTheme,
  useThemePreference,
} from "./theme";

const STORAGE_KEY = "safe-starter-kit.theme";

// Stands in for the operating-system colour scheme. The tests change osDark and fire the change listeners.
let osDark = false;
let osListeners: (() => void)[] = [];

function fireOsSchemeChange(dark: boolean): void {
  osDark = dark;
  for (const listener of osListeners) listener();
}

function isDarkClassOn(): boolean {
  return document.documentElement.classList.contains("dark");
}

beforeEach(() => {
  osDark = false;
  osListeners = [];
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string) =>
      ({
        media: query,
        get matches() {
          return osDark;
        },
        addEventListener: (_type: string, listener: () => void) => {
          osListeners.push(listener);
        },
        removeEventListener: (_type: string, listener: () => void) => {
          osListeners = osListeners.filter((candidate) => candidate !== listener);
        },
      }) as unknown as MediaQueryList,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("isThemePreference", () => {
  it.each(["light", "dark", "system"])("accepts %s", (value) => {
    expect(isThemePreference(value)).toBe(true);
  });

  it.each([["auto"], [""], [null], [undefined], [1], [{}], ["Dark"]])("rejects %j", (value) => {
    expect(isThemePreference(value)).toBe(false);
  });
});

describe("getPreference", () => {
  it("defaults to system when nothing is stored", () => {
    expect(getPreference()).toBe("system");
  });

  it("returns a stored valid preference", () => {
    window.localStorage.setItem(STORAGE_KEY, "dark");
    expect(getPreference()).toBe("dark");
  });

  it("ignores a stored value that is not a preference", () => {
    window.localStorage.setItem(STORAGE_KEY, "blue");
    expect(getPreference()).toBe("system");
  });

  it("falls back to system when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage blocked");
    });
    expect(getPreference()).toBe("system");
  });
});

describe("setPreference", () => {
  it("applies and stores dark", () => {
    setPreference("dark");
    expect(isDarkClassOn()).toBe(true);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("dark");
  });

  it("removes the dark class for light, even when the system is dark", () => {
    osDark = true;
    setPreference("light");
    expect(isDarkClassOn()).toBe(false);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("light");
  });

  it("follows a dark system when the preference is system", () => {
    osDark = true;
    setPreference("system");
    expect(isDarkClassOn()).toBe(true);
  });

  it("follows a light system when the preference is system", () => {
    osDark = false;
    document.documentElement.classList.add("dark");
    setPreference("system");
    expect(isDarkClassOn()).toBe(false);
  });

  // Known bug in theme.ts: applyTheme() re-reads the stored preference, so when storage cannot be written the
  // chosen theme is dropped and the page falls back to the system scheme. The code comment promises that the choice
  // still applies for this page view. Remove ".fails" once fixed.
  it("still applies the theme when storage cannot be written", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage blocked");
    });
    setPreference("dark");
    expect(isDarkClassOn()).toBe(true);
  });
});

describe("initTheme", () => {
  it("applies the stored preference on start", () => {
    window.localStorage.setItem(STORAGE_KEY, "dark");
    initTheme();
    expect(isDarkClassOn()).toBe(true);
  });

  it("follows operating-system changes while the preference is system", () => {
    initTheme();
    expect(isDarkClassOn()).toBe(false);

    fireOsSchemeChange(true);
    expect(isDarkClassOn()).toBe(true);

    fireOsSchemeChange(false);
    expect(isDarkClassOn()).toBe(false);
  });

  it("ignores operating-system changes while an explicit preference is set", () => {
    initTheme();
    setPreference("light");

    fireOsSchemeChange(true);
    expect(isDarkClassOn()).toBe(false);
  });
});

describe("theme hooks", () => {
  it("reports the stored preference and updates when it changes", () => {
    const { result } = renderHook(() => useThemePreference());
    expect(result.current).toBe("system");

    act(() => setPreference("dark"));
    expect(result.current).toBe("dark");
  });

  it("reports the resolved theme for a system preference", () => {
    osDark = true;
    const { result } = renderHook(() => useResolvedTheme());
    act(() => setPreference("system"));
    expect(result.current).toBe("dark");

    act(() => setPreference("light"));
    expect(result.current).toBe("light");
  });
});
