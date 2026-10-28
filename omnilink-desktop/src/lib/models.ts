import type { Message, ModelProvider } from "@/types";
import { IS_TAURI, modelAbort, modelHttp, modelStream } from "@/lib/tauri";
import { createStreamParser } from "@/lib/streamParse";

export interface ModelConfig {
  provider: ModelProvider;
  baseUrl: string;
  apiKey: string;
  model: string;
  /** Stable id used for x-opencode-session and prompt-cache routing. */
  sessionId?: string;
  /** "opencode" | "opencode-go" — Rust injects the key from the OpenCode login. */
  authSource?: string;
}

/** OpenCode Zen and Go live on dedicated inference hosts. */
export function resolveAuthSource(baseUrl: string): string | undefined {
  const base = baseUrl.toLowerCase();
  if (!base.includes("opencode.ai/inference")) return undefined;
  return base.includes("/inference/go/") ? "opencode-go" : "opencode";
}

export function isOpenCodeEndpoint(baseUrl: string): boolean {
  return baseUrl.toLowerCase().includes("opencode.ai/inference");
}

function normalizeBase(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

function isLocal(baseUrl: string): boolean {
  const base = baseUrl.toLowerCase();
  return base.includes("127.0.0.1") || base.includes("localhost") || base.includes("[::1]");
}

export function isModelConfigured(config: ModelConfig): boolean {
  const base = config.baseUrl.trim();
  if (!base) return false;
  if (config.provider === "ollama") return true;
  if (isLocal(base)) return true;
  if (config.authSource) return true;
  if (isOpenCodeEndpoint(base)) return true;
  return config.apiKey.trim().length > 0;
}

function toOpenAIMessages(messages: Message[]) {
  return messages.map((message) => ({
    role: message.role,
    content: message.content,
  }));
}

/**
 * Decide how a request authenticates.
 *
 * An explicitly pasted key always wins, so a user can point OmniLink at
 * OpenCode (or any gateway) with their own token. Otherwise OpenCode
 * endpoints fall back to the credential from the existing OpenCode login,
 * which Rust injects server-side so it never enters the webview.
 */
function transportAuth(config: ModelConfig): {
  authSource?: string;
  apiKey: string;
} {
  const manual = config.apiKey.trim();
  if (manual) return { apiKey: manual };
  const source = config.authSource ?? resolveAuthSource(config.baseUrl);
  return { authSource: source, apiKey: "" };
}

function buildHeaders(
  config: ModelConfig,
  auth: { authSource?: string; apiKey: string } = transportAuth(config),
): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "HTTP-Referer": "http://localhost:1420",
    "X-Title": "OmniLink",
  };
  if (auth.apiKey) {
    headers.Authorization = `Bearer ${auth.apiKey}`;
  }
  if (auth.authSource) {
    headers["x-opencode-session"] = config.sessionId ?? "omnilink-session";
    headers["User-Agent"] = "OmniLink/0.1.1";
  }
  return headers;
}

function chatEndpoint(config: ModelConfig): string {
  const base = normalizeBase(config.baseUrl);
  if (config.provider === "ollama") return `${base}/api/chat`;
  if (base.endsWith("/chat/completions")) return base;
  return `${base}/chat/completions`;
}

function modelsEndpoint(config: ModelConfig): string {
  const base = normalizeBase(config.baseUrl);
  if (config.provider === "ollama") return `${base}/api/tags`;
  // OpenCode lists models from the gateway root, not the chat host:
  //   https://opencode.ai/inference/go/v1/models
  //   https://opencode.ai/inference/v1/models
  if (base.includes("/inference/")) {
    const origin = new URL(base).origin;
    return base.includes("/inference/go/")
      ? `${origin}/inference/go/v1/models`
      : `${origin}/inference/v1/models`;
  }
  if (base.endsWith("/chat/completions")) {
    return base.replace(/\/chat\/completions$/, "/models");
  }
  return `${base}/models`;
}

interface OllamaTag {
  name?: string;
  model?: string;
  size?: number;
  details?: { parameter_size?: string; family?: string };
}

/** List the models a provider actually has, through the Rust transport. */
export async function listModels(config: ModelConfig): Promise<string[]> {
  const auth = transportAuth(config);
  const headers = buildHeaders(config, auth);
  const authSource = auth.authSource;

  const result = await modelHttp("GET", {
    url: modelsEndpoint(config),
    headers,
    authSource,
  });

  if (result.status < 200 || result.status >= 300) {
    throw new Error(
      `Could not list models (HTTP ${result.status}): ${result.body.slice(0, 200)}`,
    );
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(result.body);
  } catch {
    throw new Error("Model list was not valid JSON.");
  }

  if (config.provider === "ollama") {
    const tags = (parsed as { models?: OllamaTag[] }).models ?? [];
    return tags
      .map((tag) => tag.name ?? tag.model ?? "")
      .filter((name): name is string => Boolean(name));
  }

  const data = (parsed as { data?: { id?: string; name?: string }[] }).data ?? [];
  return data
    .map((model) => model.id ?? model.name ?? "")
    .filter((name): name is string => Boolean(name));
}

/** Detailed Ollama inventory, including size, so we can pick a fast default. */
export async function listOllamaModels(
  baseUrl: string,
): Promise<{ name: string; size: number; parameterSize?: string }[]> {
  const base = normalizeBase(baseUrl);
  const result = await modelHttp("GET", { url: `${base}/api/tags` });
  if (result.status < 200 || result.status >= 300) {
    throw new Error(`Ollama returned HTTP ${result.status}`);
  }
  const parsed = JSON.parse(result.body) as { models?: OllamaTag[] };
  return (parsed.models ?? [])
    .map((tag) => ({
      name: tag.name ?? tag.model ?? "",
      size: tag.size ?? 0,
      parameterSize: tag.details?.parameter_size,
    }))
    .filter((model) => Boolean(model.name));
}

/**
 * Choose a model that will actually answer quickly. Big coder models are great
 * but take minutes to load, which reads as "the app is broken".
 */
export function pickOllamaModel(
  models: { name: string; size: number }[],
): string {
  if (models.length === 0) return "";
  const chatLike = models.filter((model) =>
    /instruct|chat|coder|qwen|llama|gemma|glm|mistral|phi|deepseek|hermes|cogito|flame|iceman/i.test(
      model.name,
    ),
  );
  const pool = chatLike.length > 0 ? chatLike : models;
  const sorted = [...pool].sort((a, b) => a.size - b.size);
  return sorted[0]?.name ?? models[0].name;
}

function ollamaStatusHint(status: number, body: string): string {
  if (status === 403) {
    return "Ollama refused the request. Start it with OLLAMA_ORIGINS=* or update OmniLink.";
  }
  if (status === 404) {
    return `Model not found. Pull it first (ollama pull <model>). ${body.slice(0, 120)}`;
  }
  return `Ollama returned HTTP ${status}: ${body.slice(0, 200)}`;
}

/**
 * Stream a chat completion.
 *
 * In the desktop app this runs through Rust, because the webview origin
 * (http://tauri.localhost) is rejected by Ollama's CORS allowlist.
 */
export async function streamChat(
  config: ModelConfig,
  messages: Message[],
  onToken: (token: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const isOllama = config.provider === "ollama";
  const auth = transportAuth(config);
  const headers = buildHeaders(config, auth);
  const authSource = auth.authSource;

  let model = config.model.trim();
  if (!model || model.toLowerCase() === "auto") {
    try {
      const available = await listModels({ ...config, model: "" });
      model = isOllama
        ? pickOllamaModel(
            (await listOllamaModels(config.baseUrl)).map((entry) => ({
              name: entry.name,
              size: entry.size,
            })),
          )
        : available[0] ?? "";
    } catch {
      // Fall through to the explicit error below.
    }
  }

  if (!model) {
    throw new Error(
      isOllama
        ? "No Ollama model available. Pull one with `ollama pull llama3.2`."
        : "No model selected. Pick one in the model selector.",
    );
  }

  const requestId = `req-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const url = chatEndpoint(config);

  const body = isOllama
    ? JSON.stringify({
        model,
        messages: toOpenAIMessages(messages),
        stream: true,
        options: { temperature: 0.7 },
      })
    : JSON.stringify({
        model,
        messages: toOpenAIMessages(messages),
        stream: true,
        temperature: 0.7,
      });

  const parser = createStreamParser(isOllama ? "ollama" : "openai", onToken);
  const onChunk = (text: string) => parser.push(text);
  let transportError: string | null = null;

  const abortListener = () => {
    void modelAbort(requestId);
  };
  signal?.addEventListener("abort", abortListener);

  try {
    if (IS_TAURI) {
      await modelStream(
        requestId,
        { url, headers, body, authSource },
        {
          onChunk,
          onDone: () => undefined,
          onError: (message) => {
            transportError = message;
          },
        },
      );
    } else {
      const response = await fetch(url, {
        method: "POST",
        headers,
        body,
        signal,
      });
      if (!response.ok || !response.body) {
        const text = await response.text().catch(() => "");
        throw new Error(
          isOllama
            ? ollamaStatusHint(response.status, text)
            : `Model endpoint returned ${response.status}: ${text.slice(0, 200)}`,
        );
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        onChunk(decoder.decode(value, { stream: true }));
      }
    }
  } catch (error) {
    if (signal?.aborted) return;
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(isOllama ? ollamaStatusHint(0, message) : message);
  } finally {
    signal?.removeEventListener("abort", abortListener);
  }

  parser.flush();

  if (transportError) {
    throw new Error(transportError);
  }

  const streamError = parser.error();
  if (streamError) {
    throw new Error(streamError);
  }
}

export interface ModelTestResult {
  ok: boolean;
  model: string;
  latencyMs: number;
  reply: string;
  error?: string;
}

/** Real round-trip used by Settings → Models → Test. */
export async function testModel(config: ModelConfig): Promise<ModelTestResult> {
  const started = performance.now();
  let reply = "";
  let model = config.model || "auto";
  try {
    await streamChat(
      config,
      [{ id: "test", role: "user", content: "Reply with exactly: OK", createdAt: Date.now() }],
      (token) => {
        reply += token;
      },
    );
    return {
      ok: true,
      model,
      latencyMs: Math.round(performance.now() - started),
      reply: reply.trim().slice(0, 200),
    };
  } catch (error) {
    return {
      ok: false,
      model,
      latencyMs: Math.round(performance.now() - started),
      reply: reply.trim().slice(0, 200),
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
