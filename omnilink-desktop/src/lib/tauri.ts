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

export async function opencodeStartBackground(): Promise<string> {
  if (!IS_TAURI) return "";
  return invoke("opencode_start_background");
}

export async function opencodeStopBackground(): Promise<string> {
  if (!IS_TAURI) return "";
  return invoke("opencode_stop_background");
}

export async function opencodeStatusBackground(): Promise<boolean> {
  if (!IS_TAURI) return false;
  return invoke("opencode_status_background");
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

// ---------------------------------------------------------------------------
// Model transport (Rust-proxied, so release builds are not blocked by CORS)
// ---------------------------------------------------------------------------

export interface ModelHttpResult {
  status: number;
  body: string;
}

export interface ModelRequest {
  url: string;
  headers?: Record<string, string>;
  body?: string;
  /** "opencode" or "opencode-go": Rust injects the key from the OpenCode login. */
  authSource?: string;
}

export async function opencodeAuthStatus(): Promise<string[]> {
  if (!IS_TAURI) return [];
  try {
    return await invoke<string[]>("opencode_auth_status");
  } catch {
    return [];
  }
}

export async function modelHttp(
  method: string,
  request: ModelRequest,
): Promise<ModelHttpResult> {
  if (!IS_TAURI) {
    const response = await fetch(request.url, {
      method,
      headers: request.headers,
      body: request.body,
    });
    return { status: response.status, body: await response.text() };
  }
  return invoke<ModelHttpResult>("model_http", {
    method,
    url: request.url,
    headers: request.headers ?? {},
    body: request.body ?? null,
    authSource: request.authSource ?? null,
  });
}

export async function modelStream(
  requestId: string,
  request: ModelRequest & { body: string },
  handlers: {
    onChunk: (text: string) => void;
    onDone: () => void;
    onError: (message: string) => void;
  },
): Promise<void> {
  if (!IS_TAURI) {
    const response = await fetch(request.url, {
      method: "POST",
      headers: request.headers,
      body: request.body,
    });
    if (!response.ok || !response.body) {
      handlers.onError(`HTTP ${response.status}`);
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      handlers.onChunk(decoder.decode(value, { stream: true }));
    }
    handlers.onDone();
    return;
  }

  const { listen } = await import("@tauri-apps/api/event");

  // Rust emits the terminal event before the command returns, but event
  // delivery to JS is asynchronous. Keep the chunk listener alive until the
  // terminal event actually arrives, otherwise trailing tokens get dropped.
  let settle: () => void = () => undefined;
  const terminal = new Promise<void>((resolve) => {
    settle = resolve;
  });
  let reported = false;

  const unlisten = await Promise.all([
    listen<string>(`model:chunk:${requestId}`, (event) =>
      handlers.onChunk(event.payload),
    ),
    listen(`model:done:${requestId}`, () => {
      handlers.onDone();
      settle();
    }),
    listen<string>(`model:error:${requestId}`, (event) => {
      reported = true;
      handlers.onError(event.payload);
      settle();
    }),
  ]);

  try {
    await invoke("model_stream", {
      requestId,
      url: request.url,
      headers: request.headers ?? {},
      body: request.body,
      authSource: request.authSource ?? null,
    });
  } catch (error) {
    // Rust already emitted model:error for this; only report once.
    if (!reported) {
      reported = true;
      handlers.onError(error instanceof Error ? error.message : String(error));
    }
    settle();
  }

  // Grace period in case a terminal event is lost, so a caller never hangs.
  await Promise.race([
    terminal,
    new Promise<void>((resolve) => setTimeout(resolve, 1500)),
  ]);

  for (const stop of unlisten) stop();
}

export async function modelAbort(requestId: string): Promise<void> {
  if (!IS_TAURI) return;
  try {
    await invoke("model_abort", { requestId });
  } catch {
    // Nothing to abort.
  }
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
