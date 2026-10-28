import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import { useEffect } from "react";

import { ModalHost } from "@/components/Modals";
import { CommandPalette } from "@/components/shell/CommandPalette";
import { ContextPane } from "@/components/shell/ContextPane";
import { Rail } from "@/components/shell/Rail";
import { StatusBar } from "@/components/shell/StatusBar";
import { TitleBar } from "@/components/shell/TitleBar";
import { Toaster } from "@/components/ui/Toaster";
import { TutorialOverlay } from "@/components/tutorial/TutorialOverlay";
import { IS_TAURI, getSecret, showMainWindow } from "@/lib/tauri";
import { useResolvedTheme } from "@/lib/theme";
import { useApp } from "@/store/app";
import { AssetsView } from "@/views/AssetsView";
import { ChatView } from "@/views/ChatView";
import { CodingView } from "@/views/CodingView";
import { McpView } from "@/views/McpView";
import { PromptsView } from "@/views/PromptsView";
import { SettingsView } from "@/views/SettingsView";
import { WorkspacesView } from "@/views/WorkspacesView";

function isTypingTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(
    target.closest(
      'input, textarea, select, [contenteditable="true"], [role="textbox"]',
    ),
  );
}

export default function App() {
  const section = useApp((state) => state.section);
  const accent = useApp((state) => state.accent);
  const reduceMotion = useApp((state) => state.reduceMotion);
  const resolvedTheme = useResolvedTheme();
  const fontSize = useApp((state) => state.fontSize);
  const customCss = useApp((state) => state.customCss);
  const contextCollapsed = useApp((state) => state.contextCollapsed);
  const focusMode = useApp((state) => state.focusMode);
  const localOnlyMode = useApp((state) => state.localOnlyMode);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = resolvedTheme;
    root.dataset.accent = accent;
    root.style.colorScheme = resolvedTheme;
  }, [accent, resolvedTheme]);

  useEffect(() => {
    const root = document.documentElement;
    root.style.fontSize = `${fontSize}px`;
    // Remove old custom style
    const old = document.getElementById("custom-css");
    if (old) old.remove();
    if (customCss.trim()) {
      const style = document.createElement("style");
      style.id = "custom-css";
      style.textContent = customCss;
      document.head.appendChild(style);
    }
  }, [fontSize, customCss]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      window.setTimeout(() => void showMainWindow(), 40);
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (!localOnlyMode) return;
    const originalFetch = window.fetch;
    window.fetch = async (input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof Request
            ? input.url
            : String(input);
      const isLocal =
        url.includes("127.0.0.1") ||
        url.includes("localhost") ||
        url.startsWith("/") ||
        url.startsWith("blob:") ||
        url.startsWith("data:");
      if (!isLocal) {
        throw new Error("Blocked by local-only mode");
      }
      return originalFetch(input, init);
    };
    return () => {
      window.fetch = originalFetch;
    };
  }, [localOnlyMode]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (IS_TAURI) {
        try {
          const value = await getSecret("model.apiKey");
          if (!cancelled && value) useApp.getState().setModelApiKey(value);
        } catch {
          // Keychain unavailable; leave the in-memory key empty.
        }
      } else {
        const value = window.localStorage.getItem("omnilink.apiKey");
        if (!cancelled && value) useApp.getState().setModelApiKey(value);
      }
      // Auto-detect an Ollama model if none is configured.
      const state = useApp.getState();
      if (state.modelProvider === "ollama" && !state.modelName.trim()) {
        try {
          const res = await fetch(
            `${state.modelBaseUrl.replace(/\/+$/, "")}/api/tags`,
          );
          if (res.ok) {
            const data = (await res.json()) as { models?: { name?: string }[] };
            const first = data.models?.[0]?.name;
            if (!cancelled && first) {
              useApp.getState().setModelName(first);
            }
          }
        } catch {
          // Ollama not running; leave empty.
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const mod = event.ctrlKey || event.metaKey;
      if (!mod || event.altKey) return;
      const key = event.key.toLowerCase();
      const state = useApp.getState();

      if (key === "k") {
        event.preventDefault();
        state.setPaletteOpen(!state.paletteOpen);
        return;
      }

      if (event.shiftKey) return;

      if (key === "n") {
        if (isTypingTarget(event.target) && state.section === "chat") return;
        event.preventDefault();
        state.newConversation();
        return;
      }

      if (key === ",") {
        event.preventDefault();
        state.openSettings(state.settingsSection);
        return;
      }

      const digits: Record<string, () => void> = {
        "1": () => state.setSection("chat"),
        "2": () => state.setSection("coding"),
        "3": () => state.setSection("mcp"),
        "4": () => state.setSection("prompts"),
        "5": () => state.setSection("assets"),
        "6": () => state.setSection("workspaces"),
      };
      const navigate = digits[key];
      if (navigate) {
        event.preventDefault();
        navigate();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <MotionConfig reducedMotion={reduceMotion ? "always" : "never"}>
      <div className="app-window">
        <TitleBar />
        <div className="app-body">
          {!focusMode && <Rail />}

          {!focusMode && (
            <motion.aside
            initial={false}
            animate={{
              width: contextCollapsed ? 0 : 268,
              borderRightWidth: contextCollapsed ? 0 : 1,
            }}
            transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
            className="relative z-20 flex-none overflow-hidden border-r border-line bg-[color-mix(in_srgb,var(--bg-base)_46%,transparent)]"
            aria-hidden={contextCollapsed}
          >
            <div className="h-full w-[268px]">
              <ContextPane />
            </div>
          </motion.aside>
          )}

          <main className="app-content">
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={section}
                initial={{ opacity: 0, y: 5 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -3 }}
                transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
                className="h-full min-h-0"
              >
                {section === "chat" && <ChatView />}
                {section === "coding" && <CodingView />}
                {section === "workspaces" && <WorkspacesView />}
                {section === "mcp" && <McpView />}
                {section === "prompts" && <PromptsView />}
                {section === "assets" && <AssetsView />}
                {section === "settings" && <SettingsView />}
              </motion.div>
            </AnimatePresence>
          </main>
        </div>
        <StatusBar />
        <CommandPalette />
        <ModalHost />
        <Toaster />
        <TutorialOverlay />
      </div>
    </MotionConfig>
  );
}
