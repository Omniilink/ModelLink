// Drives the *installed/release* OmniLink webview over CDP.
//
// This is the only test that proves the shipped app can reach a model: release
// builds run on http://tauri.localhost, the exact origin Ollama answers with
// 403 when the webview calls it directly.
import { connectCDP, waitForTarget } from "./cdp.mjs";

const PORT = Number(process.env.CDP_PORT ?? 9222);
const results = [];
const record = (name, ok, detail = "") => {
  results.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : `  -> ${detail}`}`);
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const target = await waitForTarget({
  port: PORT,
  timeoutMs: 45000,
  predicate: (t) => /tauri\.localhost|localhost:1420|OmniLink/i.test(`${t.url}${t.title}`),
});

const cdp = await connectCDP(target.webSocketDebuggerUrl);
await cdp.send("Runtime.enable");
await cdp.send("Log.enable");

const consoleErrors = [];
cdp.onEvent((message) => {
  if (message.method === "Log.entryAdded" && message.params.entry.level === "error") {
    consoleErrors.push(message.params.entry.text);
  }
  if (message.method === "Runtime.exceptionThrown") {
    consoleErrors.push(
      message.params.exceptionDetails.exception?.description ??
        message.params.exceptionDetails.text,
    );
  }
});

async function evaluate(expression, { timeout = 60000 } = {}) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    timeout,
  });
  if (result.exceptionDetails) {
    throw new Error(
      result.exceptionDetails.exception?.description ??
        result.exceptionDetails.text ??
        "evaluate failed",
    );
  }
  return result.result?.value;
}

try {
  // --- 1. We are really in the packaged webview -------------------------------
  const env = await evaluate(`(() => ({
    origin: location.origin,
    href: location.href,
    hasIpc: typeof window.__TAURI_INTERNALS__?.invoke === "function",
    title: document.title,
  }))()`);
  record(
    "release webview origin is tauri.localhost",
    env.origin === "http://tauri.localhost" || env.origin === "https://tauri.localhost",
    env.origin,
  );
  record("Tauri IPC bridge is present", env.hasIpc, String(env.hasIpc));

  // --- 2. Webview fetch is genuinely blocked (the original bug) --------------
  const directFetch = await evaluate(`(async () => {
    try {
      const r = await fetch("http://127.0.0.1:11434/api/tags");
      return { ok: r.ok, status: r.status };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  })()`);
  record(
    "direct webview fetch to Ollama still blocked (Rust bypass required)",
    directFetch.ok === false,
    JSON.stringify(directFetch),
  );

  // --- 3. Rust transport works from the release webview ---------------------
  const tags = await evaluate(`(async () => {
    const r = await window.__TAURI_INTERNALS__.invoke("model_http", {
      method: "GET",
      url: "http://127.0.0.1:11434/api/tags",
      headers: {},
      body: null,
      authSource: null,
    });
    let count = 0;
    try { count = (JSON.parse(r.body).models || []).length; } catch {}
    return { status: r.status, count };
  })()`);
  record(
    "Ollama model list via Rust from release webview",
    tags.status === 200 && tags.count > 0,
    `status ${tags.status}, ${tags.count} models`,
  );

  const auth = await evaluate(
    `window.__TAURI_INTERNALS__.invoke("opencode_auth_status")`,
  );
  record(
    "OpenCode credential discovered in release build",
    Array.isArray(auth) && auth.includes("opencode-go"),
    JSON.stringify(auth),
  );

  // --- 4. Drive the real chat UI -------------------------------------------
  const token = `ZQ${Math.random().toString(36).slice(2, 6).toUpperCase()}`;
  const prompt = `Reply with only this token and nothing else: ${token}`;

  const findVisibleComposer = `(() => {
    const visible = (el) =>
      !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
    const el = [...document.querySelectorAll("textarea[data-composer]")].find(visible);
    if (!el) return { found: false };
    el.focus();
    return { found: true, focused: document.activeElement === el };
  })()`;

  let composer = await evaluate(findVisibleComposer);
  if (!composer.found) {
    const clicked = await evaluate(`(() => {
      const nav = [...document.querySelectorAll("button, a, [role='button']")].find(
        (n) => /^Chat/.test(n.textContent.trim()),
      );
      nav?.click();
      return Boolean(nav);
    })()`);
    record("chat nav reachable", clicked, String(clicked));
    await sleep(900);
    composer = await evaluate(findVisibleComposer);
  }
  record("chat composer found", composer.found, JSON.stringify(composer));

  if (composer.found) {
    // Replace any leftover draft so the token count is unambiguous.
    await evaluate(`(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      const el = [...document.querySelectorAll("textarea[data-composer]")].find(visible);
      if (!el) return false;
      el.focus();
      el.select();
      return true;
    })()`);
    await cdp.send("Input.insertText", { text: prompt });
    const typed = await evaluate(`(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      const el = [...document.querySelectorAll("textarea[data-composer]")].find(visible);
      return el ? el.value : "";
    })()`);
    record("message typed into composer", typed.includes(token), typed.slice(0, 80));

    await evaluate(`(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      const btn = [...document.querySelectorAll('[aria-label="Send message"]')].find(visible);
      if (!btn) return false;
      btn.click();
      return true;
    })()`);

    // Wait for the assistant to echo the token (user message counts once).
    let bodyText = "";
    let occurrences = 0;
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      bodyText = await evaluate(`document.body.innerText`);
      occurrences = bodyText.split(token).length - 1;
      if (occurrences >= 2) break;
      await sleep(1000);
    }

    record(
      "assistant replied in the real chat UI",
      occurrences >= 2,
      `token occurrences: ${occurrences}`,
    );

    if (occurrences < 2) {
      const snippet = bodyText.replace(/\s+/g, " ").slice(0, 600);
      console.log(`  DOM excerpt: ${snippet}`);
    } else {
      const assistantText = await evaluate(`(() => {
        const rows = [...document.querySelectorAll(".group")];
        const row = rows.reverse().find((r) => /OmniLink/.test(r.innerText));
        return row ? row.innerText.replace(/\\s+/g, " ").trim().slice(0, 200) : "";
      })()`);
      console.log(`  assistant said: ${assistantText}`);
    }
  }

  // --- 5. No runtime errors ------------------------------------------------
  // The deliberate CORS probe above is expected to log one error; it is proof
  // the bypass is needed, not an app fault.
  const appErrors = consoleErrors.filter(
    (entry) => !/11434|ERR_FAILED|CORS policy/.test(entry),
  );
  record(
    "no uncaught errors in the release webview",
    appErrors.length === 0,
    appErrors.slice(0, 3).join(" | "),
  );
} finally {
  cdp.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} release checks passed`);
process.exit(failed.length > 0 ? 1 : 0);

