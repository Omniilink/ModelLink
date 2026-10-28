import { useEffect, useState } from "react";

import { useApp } from "@/store/app";
import type { Accent, ThemeMode } from "@/types";

export type ResolvedTheme = "dark" | "light";

const prefersLight = () =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-color-scheme: light)").matches;

export function resolveTheme(
  theme: ThemeMode,
  systemPrefersLight: boolean,
): ResolvedTheme {
  if (theme === "system") return systemPrefersLight ? "light" : "dark";
  return theme;
}

export function applyAppearance(theme: ThemeMode, accent: Accent) {
  const root = document.documentElement;
  const resolved = resolveTheme(theme, prefersLight());
  root.dataset.theme = resolved;
  root.dataset.accent = accent;
  root.style.colorScheme = resolved;
  const meta = document.querySelector<HTMLMetaElement>(
    'meta[name="theme-color"]',
  );
  if (meta) {
    meta.content = resolved === "dark" ? "#0a0a0c" : "#f5f5f7";
  }
}

/** Apply persisted appearance before React paints to avoid a theme flash. */
export function bootstrapAppearance() {
  let theme: ThemeMode = "dark";
  let accent: Accent = "violet";
  try {
    const raw = localStorage.getItem("omnilink.workspace");
    if (raw) {
      const parsed = JSON.parse(raw) as {
        state?: { theme?: ThemeMode; accent?: Accent };
      };
      if (parsed.state?.theme) theme = parsed.state.theme;
      if (parsed.state?.accent) accent = parsed.state.accent;
    }
  } catch {
    // Corrupt storage falls back to the dark violet default.
  }
  applyAppearance(theme, accent);
}

export function useResolvedTheme(): ResolvedTheme {
  const theme = useApp((state) => state.theme);
  const [system, setSystem] = useState<ResolvedTheme>(() =>
    prefersLight() ? "light" : "dark",
  );

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const update = () => setSystem(media.matches ? "light" : "dark");
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return theme === "system" ? system : theme;
}
