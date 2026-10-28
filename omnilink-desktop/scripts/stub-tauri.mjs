// Stand-in for src/lib/tauri.ts so the model layer can run under Node.
// It mimics what the Rust transport does: inject OpenCode credentials and
// stream raw bytes back to the caller.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const IS_TAURI = true;

function opencodeKey(provider) {
  const authPath = path.join(
    os.homedir(),
    ".local",
    "share",
    "opencode",
    "auth.json",
  );
  if (!fs.existsSync(authPath)) return null;
  const json = JSON.parse(fs.readFileSync(authPath, "utf8"));
  return json?.[provider]?.key ?? null;
}

function withAuth(headers, authSource) {
  const merged = { ...(headers ?? {}) };
  if (authSource) {
    const key = opencodeKey(authSource);
    if (!key) throw new Error(`No OpenCode credential for '${authSource}'`);
    merged.Authorization = `Bearer ${key}`;
  }
  return merged;
}

export async function modelHttp(method, request) {
  const response = await fetch(request.url, {
    method,
    headers: withAuth(request.headers, request.authSource),
    body: request.body,
  });
  return { status: response.status, body: await response.text() };
}

export async function modelStream(_requestId, request, handlers) {
  let response;
  try {
    response = await fetch(request.url, {
      method: "POST",
      headers: withAuth(request.headers, request.authSource),
      body: request.body,
    });
  } catch (error) {
    handlers.onError(String(error));
    return;
  }

  if (!response.ok || !response.body) {
    handlers.onError(`HTTP ${response.status}: ${await response.text()}`);
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
}

export async function modelAbort() {}

export async function opencodeAuthStatus() {
  return ["opencode", "opencode-go"].filter((p) => opencodeKey(p));
}
