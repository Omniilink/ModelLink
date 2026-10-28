// Proves the OpenCode path through the real UI: open the model selector, pick a
// Go model, send a message, read the reply out of the DOM.
import { connectCDP, waitForTarget } from "./cdp.mjs";

const results = [];
const record = (name, ok, detail = "") => {
  results.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : `  -> ${detail}`}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const target = await waitForTarget({
  predicate: (t) => /tauri\.localhost/i.test(t.url),
});
const cdp = await connectCDP(target.webSocketDebuggerUrl);
await cdp.send("Runtime.enable");

const evaluate = async (expression, timeout = 60000) => {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    timeout,
  });
  if (result.exceptionDetails) {
    throw new Error(
      result.exceptionDetails.exception?.description ?? result.exceptionDetails.text,
    );
  }
  return result.result?.value;
};

const MODEL = process.env.OPENCODE_MODEL ?? "iceman-contributor-free";

try {
  // Chat view must be visible before the header chip exists.
  const ready = await evaluate(`(() => {
    const visible = (el) => !!el && el.getClientRects().length > 0;
    if ([...document.querySelectorAll("textarea[data-composer]")].some(visible)) return true;
    [...document.querySelectorAll("button, a, [role='button']")]
      .find((n) => /^Chat/.test(n.textContent.trim()))?.click();
    return false;
  })()`);
  if (!ready) await sleep(900);

  const opened = await evaluate(`(() => {
    const chip = document.querySelector("h1")?.parentElement?.querySelector("button");
    if (!chip) return false;
    chip.click();
    return true;
  })()`);
  record("model selector opened", opened, String(opened));

  // Wait for the OpenCode Go list to arrive from the network.
  let found = false;
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    found = await evaluate(`(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      return [...document.querySelectorAll("button")]
        .filter(visible)
        .some((b) => b.textContent.includes(${JSON.stringify(MODEL)}));
    })()`);
    if (found) break;
    await sleep(700);
  }
  record(`OpenCode model "${MODEL}" listed in the selector`, found);

  if (found) {
    await evaluate(`(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      const btn = [...document.querySelectorAll("button")]
        .filter(visible)
        .find((b) => b.textContent.includes(${JSON.stringify(MODEL)}));
      btn?.click();
      return Boolean(btn);
    })()`);
    await sleep(900);

    const chip = await evaluate(
      `document.querySelector("h1")?.parentElement?.querySelector("button")?.innerText ?? ""`,
    );
    record("selected model is shown in the chat header", chip.includes(MODEL), chip);

    const token = `OC${Math.random().toString(36).slice(2, 6).toUpperCase()}`;
    const prompt = `Reply with only this token and nothing else: ${token}`;

    await evaluate(`(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      const el = [...document.querySelectorAll("textarea[data-composer]")].find(visible);
      if (!el) return false;
      el.focus();
      el.select();
      return true;
    })()`);
    await cdp.send("Input.insertText", { text: prompt });

    const sent = await evaluate(`(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      const btn = [...document.querySelectorAll('[aria-label="Send message"]')].find(visible);
      if (!btn) return false;
      btn.click();
      return true;
    })()`);
    record("message sent to OpenCode", sent, String(sent));

    let occurrences = 0;
    let body = "";
    const replyDeadline = Date.now() + 180000;
    while (Date.now() < replyDeadline) {
      body = await evaluate(`document.body.innerText`);
      occurrences = body.split(token).length - 1;
      if (occurrences >= 2) break;
      await sleep(1000);
    }
    record("OpenCode replied in the real chat UI", occurrences >= 2, `token occurrences: ${occurrences}`);
    if (occurrences < 2) {
      console.log(`  DOM excerpt: ${body.replace(/\s+/g, " ").slice(-500)}`);
    }
  }
} finally {
  cdp.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} OpenCode UI checks passed`);
process.exit(failed.length > 0 ? 1 : 0);
