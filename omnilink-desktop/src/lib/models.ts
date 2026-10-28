import type { Message } from "@/types";
import type { ModelProvider } from "@/types";

export interface ModelConfig {
  provider: ModelProvider;
  baseUrl: string;
  apiKey: string;
  model: string;
}

export function isModelConfigured(config: ModelConfig): boolean {
  const base = config.baseUrl.trim();
  if (!base) return false;
  if (config.provider === "ollama") return true;
  // OpenAI-compatible: allow localhost without a key.
  if (base.includes("127.0.0.1") || base.includes("localhost")) return true;
  return config.apiKey.trim().length > 0;
}

function normalizeBase(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

function toOpenAIMessages(messages: Message[]) {
  return messages.map((message) => ({
    role: message.role,
    content: message.content,
  }));
}

export async function streamChat(
  config: ModelConfig,
  messages: Message[],
  onToken: (token: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const base = normalizeBase(config.baseUrl);

  if (config.provider === "ollama") {
    let model = config.model.trim();
    if (!model) {
      // Auto-pick the first available model.
      try {
        const res = await fetch(`${base}/api/tags`);
        if (res.ok) {
          const data = (await res.json()) as { models?: { name?: string }[] };
          model = data.models?.[0]?.name ?? "";
        }
      } catch {
        // ignore
      }
    }
    if (!model) throw new Error("No Ollama model available");
    const response = await fetch(`${base}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        messages: toOpenAIMessages(messages),
        stream: true,
      }),
      signal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`Ollama returned ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          const parsed = JSON.parse(trimmed) as {
            message?: { content?: string };
            done?: boolean;
          };
          if (parsed.message?.content) onToken(parsed.message.content);
        } catch {
          // Ignore partial or non-JSON chunks.
        }
      }
    }
    return;
  }

  // OpenAI-compatible (OpenAI, local servers, OpenCode Zen if exposed).
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (config.apiKey.trim()) {
    headers.Authorization = `Bearer ${config.apiKey.trim()}`;
  }
  const response = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: config.model.trim(),
      messages: toOpenAIMessages(messages),
      stream: true,
    }),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`Model endpoint returned ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (payload === "[DONE]") continue;
      try {
        const parsed = JSON.parse(payload) as {
          choices?: { delta?: { content?: string } }[];
        };
        const token = parsed.choices?.[0]?.delta?.content;
        if (token) onToken(token);
      } catch {
        // Ignore partial SSE frames.
      }
    }
  }
}
