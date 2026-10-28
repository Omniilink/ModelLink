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
export async function mcpProbeStdio(command: string, args: string): Promise<{ name: string; description?: string }[]> {
  if (!IS_TAURI) return [];
  return invoke("mcp_probe_stdio", { command, args });
}

export async function mcpProbeHttp(url: string): Promise<{ name: string; description?: string }[]> {
  if (!IS_TAURI) return [];
  return invoke("mcp_probe_http", { url });
}
export async function oauthStart(port: number, serverBase: string): Promise<string> {
  if (!IS_TAURI) return "";
  return invoke("oauth_start", { port, serverBase });
}

export async function oauthPoll(port: number): Promise<string | null> {
  if (!IS_TAURI) return null;
  return invoke<string | null>("oauth_poll", { port });
}

export async function oauthClear(port: number): Promise<void> {
  if (!IS_TAURI) return;
  await invoke("oauth_clear", { port });
}
export async function openUrl(url: string): Promise<void> {
  if (!IS_TAURI) {
    window.open(url, "_blank");
    return;
  }
  await invoke("open_url", { url });
}
export async function mcpCallToolStdio(command: string, args: string, toolName: string, toolArgs: string): Promise<string> {
  if (!IS_TAURI) return "";
  return invoke("mcp_call_tool_stdio", { command, args, toolName, toolArgs });
}

export async function mcpCallToolHttp(url: string, toolName: string, toolArgs: string): Promise<string> {
  if (!IS_TAURI) return "";
  return invoke("mcp_call_tool_http", { url, toolName, toolArgs });
}

export async function mcpListResourcesStdio(command: string, args: string): Promise<{ uri: string; name?: string; description?: string; mimeType?: string }[]> {
  if (!IS_TAURI) return [];
  return invoke("mcp_list_resources_stdio", { command, args });
}

export async function mcpListResourcesHttp(url: string): Promise<{ uri: string; name?: string; description?: string; mimeType?: string }[]> {
  if (!IS_TAURI) return [];
  return invoke("mcp_list_resources_http", { url });
}

export async function runShellCommand(command: string): Promise<string> {
  if (!IS_TAURI) return "";
  return invoke("run_shell_command", { command });
}

export async function httpRequest(method: string, url: string, body: string): Promise<string> {
  if (!IS_TAURI) return "";
  return invoke("http_request", { method, url, body });
}
export async function mcpListPromptsStdio(command: string, args: string): Promise<string[]> {
  if (!IS_TAURI) return [];
  return invoke("mcp_list_prompts_stdio", { command, args });
}

export async function mcpListPromptsHttp(url: string): Promise<string[]> {
  if (!IS_TAURI) return [];
  return invoke("mcp_list_prompts_http", { url });
}
export async function watchdogStart(id: string, command: string, args: string): Promise<void> {
  if (!IS_TAURI) return;
  await invoke("watchdog_start", { id, command, args });
}

export async function watchdogStop(id: string): Promise<void> {
  if (!IS_TAURI) return;
  await invoke("watchdog_stop", { id });
}

export async function watchdogStatus(id: string): Promise<{ running: boolean; restarts: number; lastError?: string }> {
  if (!IS_TAURI) return { running: false, restarts: 0 };
  return invoke("watchdog_status", { id });
}
export async function workspaceEncrypt(): Promise<void> {
  if (!IS_TAURI) return;
  await invoke("workspace_encrypt");
}

export async function workspaceDecrypt(): Promise<void> {
  if (!IS_TAURI) return;
  await invoke("workspace_decrypt");
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
