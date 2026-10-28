import { IS_TAURI } from "@/lib/tauri";

async function tauriInvoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

export const appStorage = {
  async getItem(key: string): Promise<string | null> {
    if (!IS_TAURI) {
      return window.localStorage.getItem(key);
    }
    try {
      const value = await tauriInvoke<string | null>("storage_get", { key });
      if (value == null) {
        // One-time migration from the legacy localStorage copy.
        const legacy = window.localStorage.getItem(key);
        if (legacy) {
          await tauriInvoke("storage_set", { key, value: legacy });
          window.localStorage.removeItem(key);
          return legacy;
        }
      }
      return value;
    } catch {
      return window.localStorage.getItem(key);
    }
  },
  async setItem(key: string, value: string): Promise<void> {
    if (!IS_TAURI) {
      window.localStorage.setItem(key, value);
      return;
    }
    try {
      await tauriInvoke("storage_set", { key, value });
    } catch {
      window.localStorage.setItem(key, value);
    }
  },
  async removeItem(key: string): Promise<void> {
    if (!IS_TAURI) {
      window.localStorage.removeItem(key);
      return;
    }
    try {
      await tauriInvoke("storage_delete", { key });
    } catch {
      window.localStorage.removeItem(key);
    }
  },
};

// Zustand persist storage adapter.
export const zustandStorage = {
  getItem: (name: string) => appStorage.getItem(name),
  setItem: (name: string, value: string) => appStorage.setItem(name, value),
  removeItem: (name: string) => appStorage.removeItem(name),
};
