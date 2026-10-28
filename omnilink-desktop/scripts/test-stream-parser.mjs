import { createStreamParser } from "../src/lib/streamParse.ts";

const results = [];
const check = (name, condition, detail) => {
  results.push({ name, ok: Boolean(condition), detail });
};

// --- Ollama NDJSON, split mid-object across chunks (the real failure mode) ---
{
  const tokens = [];
  const parser = createStreamParser("ollama", (t) => tokens.push(t));
  const frames =
    '{"message":{"role":"assistant","content":"OMNI"},"done":false}\n' +
    '{"message":{"role":"assistant","content":" OK"},"done":false}\n' +
    '{"message":{"role":"assistant","content":""},"done":true}\n';
  // Deliberately split in awkward places.
  parser.push(frames.slice(0, 17));
  parser.push(frames.slice(17, 61));
  parser.push(frames.slice(61));
  parser.flush();
  check("ollama ndjson split chunks", tokens.join("") === "OMNI OK", tokens.join("|"));
  check("ollama no error", parser.error() === null, parser.error());
}

// --- OpenAI SSE with split frames ---
{
  const tokens = [];
  const parser = createStreamParser("openai", (t) => tokens.push(t));
  const frames =
    'data: {"choices":[{"delta":{"content":"OMNI"}}]}\n\n' +
    'data: {"choices":[{"delta":{"content":" OK"}}]}\n\n' +
    'data: {"choices":[],"usage":{"total_tokens":3}}\n\n' +
    "data: [DONE]\n\n";
  parser.push(frames.slice(0, 9));
  parser.push(frames.slice(9, 70));
  parser.push(frames.slice(70));
  parser.flush();
  check("openai sse split chunks", tokens.join("") === "OMNI OK", tokens.join("|"));
}

// --- Reasoning-only first chunk, content later (iceman-free behaviour) ---
{
  const tokens = [];
  const parser = createStreamParser("openai", (t) => tokens.push(t));
  parser.push(
    'data: {"choices":[{"delta":{"content":"","reasoning":"thinking"}}]}\n\n',
  );
  parser.push('data: {"choices":[{"delta":{"content":"OMNI OK"}}]}\n\n');
  parser.flush();
  check("openai ignores reasoning", tokens.join("") === "OMNI OK", tokens.join("|"));
}

// --- Error surfaces instead of silently stopping ---
{
  const tokens = [];
  const parser = createStreamParser("openai", (t) => tokens.push(t));
  parser.push(
    'data: {"error":{"message":"GoUsageLimitError: Go usage limit exceeded"}}\n\n',
  );
  parser.flush();
  check(
    "openai surfaces error",
    (parser.error() ?? "").includes("Go usage limit"),
    parser.error(),
  );
}

// --- Ollama 403 body is surfaced ---
{
  const parser = createStreamParser("ollama", () => {});
  parser.push('{"error":"forbidden"}\n');
  parser.flush();
  check("ollama surfaces error", parser.error() === "forbidden", parser.error());
}

// --- Trailing frame without newline ---
{
  const tokens = [];
  const parser = createStreamParser("ollama", (t) => tokens.push(t));
  parser.push('{"message":{"content":"tail"},"done":true}');
  parser.flush();
  check("ollama flush without newline", tokens.join("") === "tail", tokens.join("|"));
}

const failed = results.filter((r) => !r.ok);
for (const r of results) {
  console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}${r.ok ? "" : `  -> ${r.detail}`}`);
}
console.log(`\n${results.length - failed.length}/${results.length} parser checks passed`);
if (failed.length > 0) process.exit(1);
