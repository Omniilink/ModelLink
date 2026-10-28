/**
 * Streaming frame parsing shared by every provider.
 *
 * Ollama streams NDJSON; OpenAI-compatible providers stream SSE. Both can split
 * a JSON object across network chunks, so parsing has to be buffered.
 */

export interface StreamParser {
  push: (chunk: string) => void;
  flush: () => void;
  error: () => string | null;
}

export function createStreamParser(
  kind: "ollama" | "openai",
  onToken: (token: string) => void,
): StreamParser {
  let buffer = "";
  let streamError: string | null = null;

  const handleLine = (raw: string) => {
    const line = raw.trim();
    if (!line) return;

    if (kind === "ollama") {
      try {
        const parsed = JSON.parse(line) as {
          message?: { content?: string };
          error?: string;
        };
        if (parsed.error) streamError = parsed.error;
        if (parsed.message?.content) onToken(parsed.message.content);
      } catch {
        // Partial NDJSON line; the rest arrives in the next chunk.
      }
      return;
    }

    if (!line.startsWith("data:")) return;
    const payload = line.slice(5).trim();
    if (!payload || payload === "[DONE]") return;

    try {
      const parsed = JSON.parse(payload) as {
        error?: { message?: string } | string;
        choices?: {
          delta?: { content?: string };
          message?: { content?: string };
        }[];
      };
      if (typeof parsed.error === "string") streamError = parsed.error;
      else if (parsed.error?.message) streamError = parsed.error.message;

      const choice = parsed.choices?.[0];
      const token = choice?.delta?.content ?? choice?.message?.content;
      if (token) onToken(token);
    } catch {
      // Partial SSE frame.
    }
  };

  return {
    push(chunk: string) {
      buffer += chunk;
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) handleLine(line);
    },
    flush() {
      if (buffer.trim()) handleLine(buffer);
      buffer = "";
    },
    error() {
      return streamError;
    },
  };
}
