import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";

export const IS_TAURI =
  typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export interface OpenCodeInfo {
  found: boolean;
  path: string | null;
  version: string | null;
}

export async function detectOpenCode(): Promise<OpenCodeInfo> {
  if (!IS_TAURI) {
    return { found: false, path: null, version: null };
  }
  return invoke<OpenCodeInfo>("detect_opencode");
}

export async function showMainWindow() {
  if (!IS_TAURI) return;
  try {
    await getCurrentWindow().show();
    await getCurrentWindow().setFocus();
  } catch {
    // The window can already be visible when the dev server reloads.
  }
}

export async function setSecret(key: string, value: string): Promise<void> {
  if (!IS_TAURI) return;
  await invoke("secret_set", { key, value });
}

export async function getSecret(key: string): Promise<string | null> {
  if (!IS_TAURI) return null;
  return invoke<string | null>("secret_get", { key });
}

export async function deleteSecret(key: string): Promise<void> {
  if (!IS_TAURI) return;
  await invoke("secret_delete", { key });
}
export function windowControls() {
  if (!IS_TAURI) return null;
  const win = getCurrentWindow();
  return {
    minimize: () => win.minimize(),
    toggleMaximize: () => win.toggleMaximize(),
    close: () => win.close(),
    isMaximized: () => win.isMaximized(),
    onResized: (handler: () => void) => win.onResized(handler),
  };
}
