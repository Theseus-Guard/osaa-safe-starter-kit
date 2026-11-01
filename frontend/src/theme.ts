import { useSyncExternalStore } from "react";

// Appearance preference. "system" follows the operating system and updates when it changes.
export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_PREFERENCES: readonly ThemePreference[] = ["light", "dark", "system"];

const STORAGE_KEY = "safe-starter-kit.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

let resolved: ResolvedTheme = "light";
const listeners = new Set<() => void>();

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === "string" && (THEME_PREFERENCES as readonly string[]).includes(value);
}

export function getPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (isThemePreference(stored)) return stored;
  } catch {
    // Storage can be blocked (for example in some private modes). Fall back to System.
  }
  return "system";
}

export function setPreference(preference: ThemePreference): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, preference);
  } catch {
    // Without storage the choice still applies for this page view only.
  }
  applyTheme(preference);
}

function applyTheme(preference: ThemePreference = getPreference()): void {
  const systemDark = window.matchMedia(DARK_QUERY).matches;
  resolved = preference === "system" ? (systemDark ? "dark" : "light") : preference;
  // The dark tokens and Tailwind's dark: variant both key off this class. color-scheme is set in index.css.
  document.documentElement.classList.toggle("dark", resolved === "dark");
  listeners.forEach((listener) => listener());
}

// Call once before the first render. Keeps the OS listener so System mode follows changes while the page is open.
export function initTheme(): void {
  applyTheme();
  window.matchMedia(DARK_QUERY).addEventListener("change", () => {
    if (getPreference() === "system") applyTheme();
  });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useThemePreference(): ThemePreference {
  return useSyncExternalStore(subscribe, getPreference, () => "system");
}

export function useResolvedTheme(): ResolvedTheme {
  return useSyncExternalStore(
    subscribe,
    () => resolved,
    () => "light",
  );
}
