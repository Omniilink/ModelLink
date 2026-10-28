/**
 * OmniLink desktop UI smoke flow.
 *
 * Drives the Vite dev server with a clean browser profile, walks every view,
 * captures screenshots, and fails on console errors or horizontal overflow.
 *
 * Usage:
 *   npm run dev            # terminal 1
 *   npm run smoke          # terminal 2
 *
 * Optional:
 *   SMOKE_URL=http://localhost:1420
 *   SMOKE_OUT=docs/screenshots
 *   BROWSER_PATH=C:\path\to\msedge.exe
 */

import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outDir = path.resolve(
  root,
  process.env.SMOKE_OUT ?? "docs/screenshots",
);
const url = process.env.SMOKE_URL ?? "http://localhost:1420/";

mkdirSync(outDir, { recursive: true });

const candidates = [
  process.env.BROWSER_PATH,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);

const executablePath = candidates.find((candidate) => existsSync(candidate));
if (!executablePath) {
  throw new Error(
    "No Chromium-based browser found. Set BROWSER_PATH to an Edge or Chrome executable.",
  );
}

const browser = await chromium.launch({
  executablePath,
  headless: process.env.SMOKE_HEADFUL !== "1",
});

const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: "dark",
});
const page = await context.newPage();

const errors = [];
page.on("console", (message) => {
  if (message.type() === "error") errors.push(message.text());
});
page.on("pageerror", (error) => errors.push(String(error)));

const settle = async (ms = 350) => {
  await page.mouse.move(1200, 760);
  await page.waitForTimeout(ms);
};

const shot = async (name) => {
  await settle(280);
  await page.screenshot({ path: path.join(outDir, `${name}.png`) });
  console.log("shot", name);
};

const fail = (message) => {
  throw new Error(message);
};

try {
  await page.addInitScript(() => {
    window.localStorage.setItem("omnilink.tutorialDone", "1");
    try {
      const raw = window.localStorage.getItem("omnilink.workspace");
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed.state) {
          parsed.state.modelName = "";
          parsed.state.modelProvider = "ollama";
          window.localStorage.setItem("omnilink.workspace", JSON.stringify(parsed));
        }
      }
    } catch {}
  });
  await page.goto(url, { waitUntil: "commit", timeout: 60000 });
  await page
    .getByText("Welcome to OmniLink", { exact: false })
    .first()
    .waitFor();
  await settle(500);
  await shot("01-chat");

  await page.keyboard.press("Control+K");
  await page.getByPlaceholder(/Search commands/).waitFor();
  await settle(400);
  await shot("02-command-palette");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);

  await page.getByRole("button", { name: "Coding", exact: true }).click();
  await page.getByText("getting-started", { exact: false }).first().waitFor();
  await page.waitForSelector(".monaco-editor", { timeout: 15_000 });
  await settle(700);
  await shot("03-coding");

  await page.getByRole("button", { name: "MCP", exact: true }).click();
  await page.getByText("Give your agents tools").waitFor();
  await settle(300);
  await shot("04-mcp-empty");

  await page
    .locator("main")
    .getByRole("button", { name: "Add your first server" })
    .click();
  await page.getByLabel("Name").fill("filesystem");
  await page.getByLabel("Command").fill("npx");
  await page
    .getByLabel("Arguments")
    .fill("-y @modelcontextprotocol/server-filesystem C:\\projects");
  await settle(400);
  await shot("05-mcp-modal");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Add server", exact: true })
    .click();
  await page.getByText("filesystem", { exact: true }).first().waitFor();
  await settle(400);
  await shot("06-mcp-server");

  await page.getByRole("button", { name: "Prompts", exact: true }).click();
  await page.getByText("Prompt library").waitFor();
  await settle(350);
  await shot("07-prompts");

  await page.keyboard.press("Control+K");
  await page.getByPlaceholder(/Search commands/).waitFor();
  await page.getByPlaceholder(/Search commands/).fill("new project");
  await settle(250);
  await page.keyboard.press("Enter");
  await page.getByLabel("Project name").waitFor();
  await settle(400);
  await shot("08-project-modal");
  await page.getByLabel("Project name").fill("desktop-demo");
  await page
    .getByLabel("Description")
    .fill("A demo project from the smoke run.");
  await page.getByRole("button", { name: "Create project" }).click();
  await page.getByText("desktop-demo", { exact: true }).first().waitFor();
  await settle(700);
  await shot("09-coding-new-project");

  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Accent color").waitFor();
  await settle(350);
  await shot("10-settings-appearance");

  await page.getByRole("button", { name: "Light" }).click();
  await page.getByRole("button", { name: "Emerald" }).click();
  await settle(450);
  await shot("11-appearance-light-emerald");

  await page.getByRole("button", { name: "Models", exact: true }).click();
  await page.getByRole("heading", { name: "OpenCode Zen" }).waitFor();
  await settle(600);
  await shot("12-settings-models");

  await page
    .getByRole("button", { name: "Data & storage", exact: true })
    .click();
  await page.getByText("Local workspace").first().waitFor();
  await settle(300);
  await shot("13-settings-data");

  await page.getByRole("button", { name: "Account", exact: true }).click();
  await page.getByText("Guest", { exact: true }).first().waitFor();
  await settle(300);
  await shot("14-settings-account");

  await page.getByRole("button", { name: "Updates", exact: true }).click();
  await page.getByRole("heading", { name: "Release channel" }).waitFor();
  await settle(300);
  await shot("15-settings-updates");

  await page.getByRole("button", { name: "About", exact: true }).click();
  await page.getByText("Copy build details").waitFor();
  await settle(300);
  await shot("16-settings-about");

  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await page.keyboard.press("Control+N");
  await settle(600);
  await shot("17-chat-empty");

  await page
    .getByPlaceholder("Ask anything, or describe what you want to build…")
    .fill("Draft a launch checklist for this app.");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2000);
  await shot("18-no-model-toast");

  const metrics = await page.evaluate(() => ({
    viewport: [innerWidth, innerHeight],
    theme: document.documentElement.dataset.theme,
    accent: document.documentElement.dataset.accent,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight,
    monaco: document.querySelectorAll(".monaco-editor").length,
  }));

  if (errors.length > 0) {
    fail(`Console errors:\n${errors.join("\n")}`);
  }
  if (metrics.scrollWidth > metrics.clientWidth) {
    fail(
      `Horizontal overflow: ${metrics.scrollWidth}px > ${metrics.clientWidth}px`,
    );
  }
  if (metrics.scrollHeight > metrics.clientHeight) {
    fail(
      `Vertical overflow: ${metrics.scrollHeight}px > ${metrics.clientHeight}px`,
    );
  }
  if (metrics.theme !== "light" || metrics.accent !== "emerald") {
    fail(
      `Appearance did not persist: theme=${metrics.theme} accent=${metrics.accent}`,
    );
  }

  console.log("METRICS", JSON.stringify(metrics));
  console.log("ERRORS", JSON.stringify(errors));
  console.log("OK", outDir);
} finally {
  await browser.close();
}
