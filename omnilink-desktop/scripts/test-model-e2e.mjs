import {
  listModels,
  pickOllamaModel,
  streamChat,
  testModel,
} from "../src/lib/models.ts";

const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : `  -> ${detail}`}`);
};

const OLLAMA = "http://127.0.0.1:11434";
const GO = "https://opencode.ai/inference/go/openai/v1";
const ZEN = "https://opencode.ai/inference/openai/v1";

// 1. Ollama model discovery through the transport layer.
let ollamaModels = [];
try {
  ollamaModels = await listModels({
    provider: "ollama",
    baseUrl: OLLAMA,
    apiKey: "",
    model: "",
  });
  record(
    "ollama listModels",
    ollamaModels.length > 0,
    `found ${ollamaModels.length}`,
  );
} catch (error) {
  record("ollama listModels", false, error.message);
}

// 2. Auto model pick chooses something sane.
if (ollamaModels.length > 0) {
  const picked = pickOllamaModel(
    ollamaModels.map((name) => ({ name, size: 0 })),
  );
  record("ollama auto pick", Boolean(picked), picked);
}

// 3. Real streaming reply from Ollama.
{
  let reply = "";
  const started = Date.now();
  try {
    await streamChat(
      {
        provider: "ollama",
        baseUrl: OLLAMA,
        apiKey: "",
        model: "merge-gemma-2b:latest",
        sessionId: "omnilink-e2e",
      },
      [
        {
          id: "1",
          role: "user",
          content: "Reply with exactly: OMNI OK",
          createdAt: Date.now(),
        },
      ],
      (token) => {
        reply += token;
      },
    );
    record(
      "ollama streamChat",
      reply.trim().length > 0,
      `${Date.now() - started}ms -> ${JSON.stringify(reply.trim())}`,
    );
  } catch (error) {
    record("ollama streamChat", false, error.message);
  }
}

// 4. OpenCode Go model discovery.
let goModels = [];
try {
  goModels = await listModels({
    provider: "custom",
    baseUrl: GO,
    apiKey: "",
    model: "",
    authSource: "opencode-go",
  });
  record("opencode go listModels", goModels.length > 0, `found ${goModels.length}`);
} catch (error) {
  record("opencode go listModels", false, error.message);
}

// 5. OpenCode Go real streaming reply (free model, no usage limit).
{
  let reply = "";
  try {
    await streamChat(
      {
        provider: "custom",
        baseUrl: GO,
        apiKey: "",
        model: "iceman-contributor-free",
        authSource: "opencode-go",
        sessionId: "omnilink-e2e-go",
      },
      [
        {
          id: "1",
          role: "user",
          content: "Reply with exactly: OMNI OK",
          createdAt: Date.now(),
        },
      ],
      (token) => {
        reply += token;
      },
    );
    record(
      "opencode go streamChat",
      reply.trim().length > 0,
      JSON.stringify(reply.trim()),
    );
  } catch (error) {
    record("opencode go streamChat", false, error.message);
  }
}

// 6. Quota errors must surface, not vanish.
{
  const result = await testModel({
    provider: "custom",
    baseUrl: GO,
    apiKey: "",
    model: "deepseek-v4.1-flash",
    authSource: "opencode-go",
    sessionId: "omnilink-e2e-quota",
  });
  record(
    "go quota error surfaces",
    !result.ok && /usage limit/i.test(result.error ?? ""),
    result.error ?? "(no error)",
  );
}

// 7. Missing credential must produce a useful message.
{
  const result = await testModel({
    provider: "custom",
    baseUrl: ZEN,
    apiKey: "",
    model: "big-pickle",
    authSource: "opencode",
    sessionId: "omnilink-e2e-zen",
  });
  record(
    "missing zen credential is explicit",
    !result.ok && /credential/i.test(result.error ?? ""),
    result.error ?? "(no error)",
  );
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} live checks passed`);
if (failed.length > 0) process.exit(1);
