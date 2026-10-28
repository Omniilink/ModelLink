import {
  AlertCircle,
  Check,
  CheckCircle2,
  Copy,
  Cpu,
  Database,
  Download,
  HardDrive,
  Loader2,
  Lock,
  Monitor,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  WifiOff,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Switch } from "@/components/ui/Switch";
import { Segmented } from "@/components/ui/Segmented";
import {
  detectOpenCode,
  IS_TAURI,
  deleteSecret,
  oauthClear,
  oauthPoll,
  oauthStart,
  openUrl,
  setSecret,
  type OpenCodeInfo,
} from "@/lib/tauri";
import { useResolvedTheme } from "@/lib/theme";
import {
  copyText,
  downloadJson,
  formatBytes,
  pluralize,
} from "@/lib/utils";
import { useApp } from "@/store/app";
import type { Accent, ModelProvider, SettingsSection, ThemeMode } from "@/types";

const sectionTitles: Record<SettingsSection, string> = {
  appearance: "Appearance",
  models: "Models",
  data: "Data & storage",
  account: "Account",
  updates: "Updates",
  about: "About",
};

const sectionDescriptions: Record<SettingsSection, string> = {
  appearance: "Theme, accent, and motion preferences",
  models: "Connect the models that power chat and coding",
  data: "Local workspace data and exports",
  account: "Identity, sync, and connected services",
  updates: "Version and release channel",
  about: "Build details and project information",
};

function Group({ children }: { children: ReactNode }) {
  return (
    <div className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface">
      {children}
    </div>
  );
}

function Row({
  title,
  description,
  children,
  icon,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-4 px-4 py-3.5">
      {icon && (
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-subtle">
          {icon}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <p className="text-[12px] font-medium text-fg">{title}</p>
        {description && (
          <p className="mt-0.5 text-[10.5px] leading-relaxed text-subtle">
            {description}
          </p>
        )}
      </div>
      {children && <div className="flex flex-none items-center gap-2">{children}</div>}
    </div>
  );
}

function GroupLabel({ children }: { children: ReactNode }) {
  return (
    <h3 className="mb-2 text-[9.5px] font-semibold uppercase tracking-[0.15em] text-subtle">
      {children}
    </h3>
  );
}

const accents: { id: Accent; label: string; color: string }[] = [
  { id: "violet", label: "Violet", color: "#8b5cf6" },
  { id: "cyan", label: "Cyan", color: "#06b6d4" },
  { id: "emerald", label: "Emerald", color: "#10b981" },
  { id: "rose", label: "Rose", color: "#f43f5e" },
  { id: "amber", label: "Amber", color: "#f59e0b" },
];

function ThemePreview({ mode }: { mode: ThemeMode }) {
  const dark = mode !== "light";
  const light = mode !== "dark";
  const background =
    mode === "system"
      ? "linear-gradient(135deg, #0b0b0f 0%, #0b0b0f 49%, #f2f2f5 50%, #f2f2f5 100%)"
      : dark
        ? "#0b0b0f"
        : "#f2f2f5";
  const panel = dark ? "#17171c" : "#ffffff";
  const line = dark ? "#26262d" : "#e3e3e8";

  return (
    <div
      className="relative h-[70px] overflow-hidden rounded-lg border border-line"
      style={{ background }}
    >
      <div
        className="absolute left-0 top-0 flex h-3.5 w-full items-center gap-1 border-b px-1.5"
        style={{ background: panel, borderColor: line }}
      >
        <span className="h-1 w-1 rounded-full bg-[#f87171]/70" />
        <span className="h-1 w-1 rounded-full bg-[#fbbf24]/70" />
        <span className="h-1 w-1 rounded-full bg-[#34d399]/70" />
      </div>
      <div
        className="absolute bottom-0 left-0 top-3.5 w-[22%] border-r"
        style={{ background: panel, borderColor: line }}
      />
      <div className="absolute left-[30%] right-[8%] top-[27%] space-y-1.5">
        <div
          className="h-1.5 w-[42%] rounded-full"
          style={{ background: dark ? "#b9a4ff" : "#7c3aed", opacity: 0.9 }}
        />
        <div
          className="h-1 w-[78%] rounded-full"
          style={{ background: dark ? "#3a3a45" : "#d5d5dc" }}
        />
        <div
          className="h-1 w-[64%] rounded-full"
          style={{ background: dark ? "#3a3a45" : "#d5d5dc" }}
        />
        <div
          className="h-4 w-[50%] rounded"
          style={{
            background: light && !dark ? "#f0f0f4" : "#202027",
            border: `1px solid ${line}`,
          }}
        />
      </div>
    </div>
  );
}

function ThemeCard({
  mode,
  label,
  active,
  onSelect,
}: {
  mode: ThemeMode;
  label: string;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={
        active
          ? "rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-1.5 text-left"
          : "rounded-xl border border-line bg-[var(--bg-base)] p-1.5 text-left transition-colors hover:border-line-strong"
      }
    >
      <ThemePreview mode={mode} />
      <div className="flex items-center gap-1.5 px-1 pb-0.5 pt-2">
        <span className="text-[10.5px] font-medium text-fg">{label}</span>
        <span className="flex-1" />
        {active && <Check size={11} className="text-accent-text" />}
      </div>
    </button>
  );
}

function AppearanceSettings() {
  const theme = useApp((state) => state.theme);
  const setTheme = useApp((state) => state.setTheme);
  const accent = useApp((state) => state.accent);
  const setAccent = useApp((state) => state.setAccent);
  const reduceMotion = useApp((state) => state.reduceMotion);
  const setReduceMotion = useApp((state) => state.setReduceMotion);
  const contextCollapsed = useApp((state) => state.contextCollapsed);
  const toggleContext = useApp((state) => state.toggleContext);

  return (
    <div className="space-y-7">
      <section>
        <GroupLabel>Theme</GroupLabel>
        <div className="grid grid-cols-3 gap-2.5">
          <ThemeCard
            mode="system"
            label="System"
            active={theme === "system"}
            onSelect={() => setTheme("system")}
          />
          <ThemeCard
            mode="dark"
            label="Dark"
            active={theme === "dark"}
            onSelect={() => setTheme("dark")}
          />
          <ThemeCard
            mode="light"
            label="Light"
            active={theme === "light"}
            onSelect={() => setTheme("light")}
          />
        </div>
      </section>

      <section>
        <GroupLabel>Accent color</GroupLabel>
        <div className="flex flex-wrap gap-2">
          {accents.map((item) => {
            const active = accent === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setAccent(item.id)}
                aria-pressed={active}
                className={
                  active
                    ? "flex items-center gap-2 rounded-lg border border-line-strong bg-[var(--bg-elevated)] px-2.5 py-1.5"
                    : "flex items-center gap-2 rounded-lg border border-line bg-transparent px-2.5 py-1.5 transition-colors hover:bg-[var(--bg-hover)]"
                }
              >
                <span
                  className="flex h-4 w-4 items-center justify-center rounded-full"
                  style={{
                    background: item.color,
                    boxShadow: active
                      ? `0 0 0 2px var(--bg-base), 0 0 0 3.5px ${item.color}`
                      : undefined,
                  }}
                >
                  {active && (
                    <Check
                      size={9}
                      strokeWidth={3}
                      className={
                        item.id === "amber" || item.id === "cyan"
                          ? "text-black/70"
                          : "text-white"
                      }
                    />
                  )}
                </span>
                <span className="text-[10.5px] font-medium text-muted">
                  {item.label}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      <section>
        <GroupLabel>Behavior</GroupLabel>
        <Group>
          <Row
            title="Reduce motion"
            description="Limit view transitions and interface animations."
          >
            <Switch
              checked={reduceMotion}
              onChange={setReduceMotion}
              label="Reduce motion"
            />
          </Row>
          <Row
            title="Sidebar"
            description="Show the contextual pane beside the icon rail."
          >
            <Switch
              checked={!contextCollapsed}
              onChange={toggleContext}
              label="Show sidebar"
            />
          </Row>
        </Group>
      </section>

      <section>
        <GroupLabel>Preview</GroupLabel>
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-[var(--bg-surface)] p-4">
          <button type="button" className="btn btn-primary">
            Primary action
          </button>
          <button type="button" className="btn">
            Secondary
          </button>
          <span className="rounded-full border border-[var(--accent-border)] bg-[var(--accent-soft)] px-2 py-0.5 text-[9.5px] font-medium text-accent-text">
            Accent chip
          </span>
          <span className="flex items-center gap-1.5 text-[10.5px] text-subtle">
            <span className="h-1.5 w-1.5 rounded-full bg-success" />
            Connected
          </span>
        </div>
      </section>
    </div>
  );
}

function ModelsSettings() {
  const providers = useApp((state) => state.providers);
  const activeProviderId = useApp((state) => state.activeProviderId);
  const addProvider = useApp((state) => state.addProvider);
  const deleteProvider = useApp((state) => state.deleteProvider);
  const setActiveProvider = useApp((state) => state.setActiveProvider);
  const [newName, setNewName] = useState("");
  const [newUrl, setNewUrl] = useState("");
  const [newModel, setNewModel] = useState("");
  const [newKey, setNewKey] = useState("");
  const ollamaUrl = useApp((state) => state.ollamaUrl);
  const setOllamaUrl = useApp((state) => state.setOllamaUrl);
  const modelProvider = useApp((state) => state.modelProvider);
  const modelBaseUrl = useApp((state) => state.modelBaseUrl);
  const modelApiKey = useApp((state) => state.modelApiKey);
  const modelName = useApp((state) => state.modelName);
  const setModelProvider = useApp((state) => state.setModelProvider);
  const setModelBaseUrl = useApp((state) => state.setModelBaseUrl);
  const setModelApiKey = useApp((state) => state.setModelApiKey);
  const setModelName = useApp((state) => state.setModelName);
  const pushToast = useApp((state) => state.pushToast);
  const [opencode, setOpencode] = useState<{
    status: "idle" | "desktop" | "checking" | "found" | "missing";
    info?: OpenCodeInfo;
    error?: string;
  }>({ status: "idle" });
  const [ollama, setOllama] = useState<{
    status: "idle" | "checking" | "online" | "offline";
    models?: string[];
    error?: string;
  }>({ status: "idle" });
  const [copiedCommand, setCopiedCommand] = useState(false);

  const checkOpenCode = async () => {
    setOpencode({ status: "checking" });
    try {
      const info = await detectOpenCode();
      setOpencode({ status: info.found ? "found" : "missing", info });
    } catch (error) {
      setOpencode({
        status: "missing",
        error: error instanceof Error ? error.message : "Detection failed",
      });
    }
  };

  useEffect(() => {
    if (!IS_TAURI) {
      setOpencode({ status: "desktop" });
      return;
    }
    void checkOpenCode();
    // Detection only runs when the Models pane opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const checkOllama = async () => {
    setOllama({ status: "checking" });
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 3500);
    try {
      const response = await fetch(
        `${ollamaUrl.trim().replace(/\/+$/, "")}/api/tags`,
        { signal: controller.signal },
      );
      if (!response.ok) {
        throw new Error(`Ollama returned ${response.status}`);
      }
      const data = (await response.json()) as {
        models?: { name?: string }[];
      };
      const models = (data.models ?? [])
        .map((model) => model.name)
        .filter((name): name is string => Boolean(name));
      setOllama({ status: "online", models });
    } catch (error) {
      setOllama({
        status: "offline",
        error:
          error instanceof Error && error.name !== "AbortError"
            ? error.message
            : "No response before the timeout.",
      });
    } finally {
      window.clearTimeout(timer);
    }
  };

  const copyCommand = async () => {
    try {
      await copyText("npm install -g opencode-ai");
      setCopiedCommand(true);
      window.setTimeout(() => setCopiedCommand(false), 1500);
    } catch {
      pushToast({ title: "Could not access the clipboard", tone: "danger" });
    }
  };

  const opencodeBadge =
    !IS_TAURI || opencode.status === "desktop" ? (
      <span className="flex items-center gap-1.5 rounded-full border border-line bg-[var(--bg-hover)] px-2 py-0.5 text-[9.5px] font-medium text-subtle">
        <Monitor size={10} />
        Desktop only
      </span>
    ) : opencode.status === "checking" ? (
      <span className="flex items-center gap-1.5 text-[10px] text-subtle">
        <Loader2 size={11} className="animate-spin" />
        Detecting
      </span>
    ) : opencode.status === "found" ? (
      <span className="flex items-center gap-1.5 rounded-full border border-[color-mix(in_srgb,var(--success)_30%,transparent)] bg-[color-mix(in_srgb,var(--success)_10%,transparent)] px-2 py-0.5 text-[9.5px] font-medium text-success">
        <CheckCircle2 size={10} />
        Detected
      </span>
    ) : opencode.status === "missing" ? (
      <span className="flex items-center gap-1.5 rounded-full border border-line bg-[var(--bg-hover)] px-2 py-0.5 text-[9.5px] font-medium text-subtle">
        <AlertCircle size={10} />
        Not found
      </span>
    ) : null;

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-line bg-[var(--bg-surface)] p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-[12.5px] font-medium text-fg">Providers</h3>
            <p className="mt-1 max-w-[560px] text-[10.5px] leading-relaxed text-subtle">
              Manage multiple AI providers. Keys are stored in the OS keychain.
            </p>
          </div>
        </div>

        {providers.length === 0 ? (
          <p className="mt-3 text-[10.5px] text-subtle">
            No providers configured yet. Add one below.
          </p>
        ) : (
          <div className="mt-3 space-y-2">
            {providers.map((provider) => (
              <div
                key={provider.id}
                className="flex items-center gap-3 rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-[12px] font-medium text-fg">
                      {provider.name}
                    </span>
                    {activeProviderId === provider.id && (
                      <span className="rounded border border-[var(--accent-border)] bg-[var(--accent-soft)] px-1.5 py-px text-[8.5px] font-semibold uppercase tracking-wide text-accent-text">
                        active
                      </span>
                    )}
                    {provider.hasKey && (
                      <span className="text-[9px] text-success">key set</span>
                    )}
                  </div>
                  <p className="truncate font-mono text-[9.5px] text-subtle">
                    {provider.baseUrl} · {provider.model}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setActiveProvider(provider.id)}
                  className="btn h-7 text-[10px]"
                >
                  Use
                </button>
                <button
                  type="button"
                  onClick={() => deleteProvider(provider.id)}
                  className="btn btn-danger h-7 text-[10px]"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="mt-4 grid gap-2 sm:grid-cols-2">
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Name (e.g. Groq)"
            className="field h-8 text-[11px]"
          />
          <input
            value={newUrl}
            onChange={(e) => setNewUrl(e.target.value)}
            placeholder="Base URL"
            className="field h-8 text-[11px]"
          />
          <input
            value={newModel}
            onChange={(e) => setNewModel(e.target.value)}
            placeholder="Model"
            className="field h-8 text-[11px]"
          />
          <input
            type="password"
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder="API key"
            className="field h-8 text-[11px]"
          />
        </div>
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={() => {
              if (!newName.trim() || !newUrl.trim() || !newModel.trim()) {
                pushToast({ title: "Name, URL, and model required", tone: "danger" });
                return;
              }
              const id = addProvider({
                name: newName.trim(),
                baseUrl: newUrl.trim(),
                model: newModel.trim(),
                hasKey: Boolean(newKey.trim()),
              });
              if (newKey.trim()) {
                void setSecret(`provider.${id}.apiKey`, newKey.trim()).catch(
                  () => undefined,
                );
              }
              setActiveProvider(id);
              setNewName("");
              setNewUrl("");
              setNewModel("");
              setNewKey("");
              pushToast({ title: "Provider added", tone: "success" });
            }}
            className="btn btn-primary h-7 text-[10.5px]"
          >
            Add provider
          </button>
        </div>
      </div>

      <div className="rounded-xl border border-line bg-[var(--bg-surface)] p-4">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 flex-none items-center justify-center rounded-lg border border-line bg-[var(--bg-elevated)] text-accent-text">
            <Cpu size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="text-[12.5px] font-medium text-fg">
              Model connection
            </h3>
            <p className="mt-1 max-w-[560px] text-[10.5px] leading-relaxed text-subtle">
              Connects chat to a model. Keys stay on this device; Ollama and
              OpenAI-compatible endpoints (including OpenCode Zen) are supported.
            </p>

            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              <label className="flex flex-col gap-1">
                <span className="text-[10px] font-medium text-muted">
                  Provider
                </span>
                <select
                  value={modelProvider}
                  onChange={(event) =>
                    setModelProvider(event.target.value as ModelProvider)
                  }
                  className="field h-8 text-[11px]"
                >
                  <option value="ollama">Ollama (local)</option>
                  <option value="openai">OpenAI</option>
                  <option value="custom">Custom (OpenAI-compatible)</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] font-medium text-muted">Model</span>
                <input
                  value={modelName}
                  onChange={(event) => setModelName(event.target.value)}
                  placeholder={
                    modelProvider === "ollama" ? "llama3.2" : "gpt-4o-mini"
                  }
                  className="field h-8 text-[11px]"
                />
              </label>
            </div>

            <label className="mt-2 flex flex-col gap-1">
              <span className="text-[10px] font-medium text-muted">
                Base URL
              </span>
              <input
                value={modelBaseUrl}
                onChange={(event) => setModelBaseUrl(event.target.value)}
                placeholder="http://127.0.0.1:11434"
                className="field h-8 text-[11px]"
              />
            </label>

            {modelProvider !== "ollama" && (
              <label className="mt-2 flex flex-col gap-1">
                <span className="text-[10px] font-medium text-muted">
                  API key
                </span>
                <input
                  type="password"
                  value={modelApiKey}
                  onChange={(event) => setModelApiKey(event.target.value)}
                  placeholder="sk-…"
                  className="field h-8 text-[11px]"
                />
              </label>
            )}

            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  const configured =
                    modelBaseUrl.trim() &&
                    modelName.trim() &&
                    (modelProvider === "ollama" ||
                      modelApiKey.trim() ||
                      modelBaseUrl.includes("127.0.0.1") ||
                      modelBaseUrl.includes("localhost"));
                  if (configured) {
                    if (IS_TAURI) {
                      void setSecret(
                        "model.apiKey",
                        modelApiKey,
                      ).catch(() => undefined);
                      if (!modelApiKey.trim()) {
                        void deleteSecret("model.apiKey").catch(
                          () => undefined,
                        );
                      }
                    } else {
                      if (modelApiKey.trim()) {
                        window.localStorage.setItem(
                          "omnilink.apiKey",
                          modelApiKey,
                        );
                      } else {
                        window.localStorage.removeItem("omnilink.apiKey");
                      }
                    }
                  }
                  pushToast({
                    title: configured
                      ? "Model connection saved"
                      : "Missing required fields",
                    tone: configured ? "success" : "danger",
                  });
                }}
                className="btn h-7 text-[10.5px]"
              >
                Save connection
              </button>
              <span className="text-[9.5px] text-subtle">
                {modelProvider === "ollama"
                  ? "Streams from a local Ollama instance."
                  : "Uses an OpenAI-compatible chat completions endpoint."}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-line bg-[var(--bg-surface)] p-4">
        <div className="flex items-start gap-3">
          <span className="relative flex h-9 w-9 flex-none items-center justify-center rounded-lg border border-line bg-[var(--bg-elevated)] text-accent-text">
            <Sparkles size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-[12.5px] font-medium text-fg">
                OpenCode Zen
              </h3>
              <span className="rounded border border-[var(--accent-border)] bg-[var(--accent-soft)] px-1.5 py-px text-[8.5px] font-semibold uppercase tracking-wide text-accent-text">
                free tier
              </span>
              {opencodeBadge}
            </div>
            <p className="mt-1 max-w-[560px] text-[10.5px] leading-relaxed text-subtle">
              Free models through OpenCode's local server. OmniLink detects an
              existing install and manages the process — no API key required
              for the free tier.
            </p>

            {opencode.status === "found" && opencode.info && (
              <div className="mt-3 rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2">
                <div className="flex items-center gap-2 text-[10px]">
                  <CheckCircle2 size={11} className="flex-none text-success" />
                  <span className="font-medium text-fg">
                    OpenCode {opencode.info.version ?? "installed"}
                  </span>
                </div>
                <p
                  className="mt-1 truncate font-mono text-[9.5px] text-subtle"
                  title={opencode.info.path ?? ""}
                >
                  {opencode.info.path}
                </p>
              </div>
            )}

            {(opencode.status === "missing" ||
              opencode.status === "desktop") && (
              <div className="mt-3 rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2.5">
                <p className="text-[10px] leading-relaxed text-subtle">
                  {IS_TAURI
                    ? "Install OpenCode once, then run detection again."
                    : "OpenCode detection and process management run in the OmniLink desktop app."}
                </p>
                {IS_TAURI && (
                  <div className="mt-2 flex items-center gap-2 rounded-md border border-line bg-[var(--bg-elevated)] px-2 py-1.5">
                    <code className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted">
                      npm install -g opencode-ai
                    </code>
                    <button
                      type="button"
                      onClick={() => void copyCommand()}
                      className="flex flex-none items-center gap-1 rounded px-1.5 py-0.5 text-[9.5px] text-subtle transition-colors hover:bg-hover hover:text-fg"
                    >
                      {copiedCommand ? (
                        <Check size={10} className="text-success" />
                      ) : (
                        <Copy size={10} />
                      )}
                      {copiedCommand ? "Copied" : "Copy"}
                    </button>
                  </div>
                )}
              </div>
            )}

            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={() => void checkOpenCode()}
                disabled={!IS_TAURI || opencode.status === "checking"}
                title={
                  IS_TAURI
                    ? "Rescan PATH and the npm global directory"
                    : "Available in the desktop app"
                }
                className="btn h-7 text-[10.5px]"
              >
                <RefreshCw
                  size={11}
                  className={opencode.status === "checking" ? "animate-spin" : ""}
                />
                {opencode.status === "found" ? "Check again" : "Detect OpenCode"}
              </button>
              <span className="text-[9.5px] text-subtle">
                {IS_TAURI
                  ? "Rescans PATH and the npm global directory."
                  : "Runs natively in the desktop build."}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-line bg-[var(--bg-surface)] p-4">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 flex-none items-center justify-center rounded-lg border border-line bg-[var(--bg-elevated)] text-muted">
            <Cpu size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-[12.5px] font-medium text-fg">Ollama</h3>
              <span className="rounded border border-line px-1.5 py-px text-[8.5px] font-semibold uppercase tracking-wide text-subtle">
                local
              </span>
              {ollama.status === "online" && (
                <span className="flex items-center gap-1.5 rounded-full border border-[color-mix(in_srgb,var(--success)_30%,transparent)] bg-[color-mix(in_srgb,var(--success)_10%,transparent)] px-2 py-0.5 text-[9.5px] font-medium text-success">
                  <span className="h-1.5 w-1.5 rounded-full bg-success" />
                  Reachable
                </span>
              )}
              {ollama.status === "offline" && (
                <span className="flex items-center gap-1.5 rounded-full border border-line bg-[var(--bg-hover)] px-2 py-0.5 text-[9.5px] font-medium text-subtle">
                  <WifiOff size={10} />
                  Offline
                </span>
              )}
            </div>
            <p className="mt-1 max-w-[560px] text-[10.5px] leading-relaxed text-subtle">
              Run models entirely on this machine. Start Ollama, then check the
              endpoint. Nothing leaves your computer.
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <input
                value={ollamaUrl}
                onChange={(event) => setOllamaUrl(event.target.value)}
                placeholder="http://127.0.0.1:11434"
                className="field h-7 w-[240px] font-mono text-[10.5px]"
                spellCheck={false}
              />
              <button
                type="button"
                onClick={() => void checkOllama()}
                disabled={ollama.status === "checking"}
                className="btn h-7 text-[10.5px]"
              >
                {ollama.status === "checking" ? (
                  <Loader2 size={11} className="animate-spin" />
                ) : (
                  <RefreshCw size={11} />
                )}
                Check connection
              </button>
            </div>
            {ollama.status === "online" && (
              <div className="mt-3 rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2">
                <p className="text-[10px] font-medium text-fg">
                  {ollama.models?.length ?? 0}{" "}
                  {pluralize(ollama.models?.length ?? 0, "model")} available
                </p>
                <p className="mt-0.5 truncate font-mono text-[9.5px] text-subtle">
                  {ollama.models?.slice(0, 5).join(" · ") || "No models installed"}
                </p>
              </div>
            )}
            {ollama.status === "offline" && (
              <p className="mt-3 rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2 text-[10px] leading-relaxed text-subtle">
                Could not reach {ollamaUrl}. {ollama.error}
              </p>
            )}
          </div>
        </div>
      </div>

      <div className="flex items-start gap-3 rounded-xl border border-line bg-[color-mix(in_srgb,var(--bg-surface)_60%,transparent)] p-4">
        <Lock size={14} className="mt-0.5 flex-none text-subtle" />
        <p className="text-[10px] leading-relaxed text-subtle">
          Provider keys will be stored in the operating system keychain, never
          in the workspace database. The model runtime connects these providers
          to chat and coding sessions in the next milestone.
        </p>
      </div>
    </div>
  );
}

function DataSettings() {
  const conversations = useApp((state) => state.conversations);
  const projects = useApp((state) => state.projects);
  const prompts = useApp((state) => state.prompts);
  const mcpServers = useApp((state) => state.mcpServers);
  const resetWorkspace = useApp((state) => state.resetWorkspace);
  const pushToast = useApp((state) => state.pushToast);
  const [confirmReset, setConfirmReset] = useState(false);

  const size = useMemo(
    () =>
      new Blob([
        JSON.stringify({ conversations, projects, prompts, mcpServers }),
      ]).size,
    [conversations, mcpServers, projects, prompts],
  );

  const exportWorkspace = () => {
    downloadJson(`omnilink-workspace-${new Date().toISOString().slice(0, 10)}.json`, {
      app: "OmniLink Desktop",
      version: 1,
      exportedAt: new Date().toISOString(),
      conversations,
      projects,
      prompts,
      mcpServers,
    });
    pushToast({ title: "Workspace exported", tone: "success" });
  };

  const stats = [
    { label: "Chats", value: conversations.length, icon: Database },
    { label: "Projects", value: projects.length, icon: HardDrive },
    { label: "Prompts", value: prompts.length, icon: Sparkles },
    { label: "MCP servers", value: mcpServers.length, icon: Cpu },
  ];

  return (
    <div className="space-y-7">
      <section>
        <GroupLabel>Local workspace</GroupLabel>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {stats.map((stat) => {
            const Icon = stat.icon;
            return (
              <div
                key={stat.label}
                className="rounded-xl border border-line bg-[var(--bg-surface)] px-3.5 py-3"
              >
                <div className="flex items-center gap-1.5 text-subtle">
                  <Icon size={11} />
                  <span className="text-[9.5px] font-medium uppercase tracking-[0.1em]">
                    {stat.label}
                  </span>
                </div>
                <p className="mt-1.5 text-[19px] font-semibold tabular-nums tracking-tight text-fg">
                  {stat.value}
                </p>
              </div>
            );
          })}
        </div>
        <div className="mt-2 flex items-center justify-between rounded-xl border border-line bg-[var(--bg-surface)] px-4 py-3">
          <div>
            <p className="text-[11.5px] font-medium text-fg">
              Workspace data
            </p>
            <p className="mt-0.5 text-[10px] text-subtle">
              Chats, projects, prompts, and MCP configuration
            </p>
          </div>
          <span className="font-mono text-[11px] text-muted">
            {formatBytes(size)}
          </span>
        </div>
      </section>

      <section>
        <GroupLabel>Export & reset</GroupLabel>
        <Group>
          <Row
            title="Export workspace"
            description="Download a JSON archive of conversations, projects, prompts, and MCP servers."
            icon={<Download size={13} />}
          >
            <button
              type="button"
              className="btn h-7 text-[10.5px]"
              onClick={exportWorkspace}
            >
              Export JSON
            </button>
          </Row>
          <Row
            title="Reset local data"
            description="Restore the welcome workspace and remove all local chats, projects, prompts, and servers."
            icon={<RotateCcw size={13} />}
          >
            <button
              type="button"
              className="btn btn-danger h-7 text-[10.5px]"
              onClick={() => setConfirmReset(true)}
            >
              Reset
            </button>
          </Row>
        </Group>
      </section>

      <div className="flex items-start gap-3 rounded-xl border border-line bg-[var(--bg-surface)] p-4">
        <ShieldCheck size={14} className="mt-0.5 flex-none text-success" />
        <p className="text-[10px] leading-relaxed text-subtle">
          OmniLink is local-first. Workspace data stays on this device, and
          exports are generated entirely in the app. No telemetry is sent by
          default.
        </p>
      </div>

      <ConfirmDialog
        open={confirmReset}
        title="Reset local data?"
        description="Every conversation, project, prompt, and MCP server will be replaced with the welcome workspace."
        confirmLabel="Reset workspace"
        onClose={() => setConfirmReset(false)}
        onConfirm={() => {
          resetWorkspace();
          pushToast({
            title: "Workspace reset",
            description: "The welcome workspace has been restored.",
          });
        }}
      />
    </div>
  );
}

function AccountSettings() {
  const pushToast = useApp((state) => state.pushToast);
  const [connecting, setConnecting] = useState(false);
  const [serverUrl, setServerUrl] = useState(
    "https://omniilink.dpdns.org",
  );

  const connectGoogle = async () => {
    if (!IS_TAURI) {
      pushToast({
        title: "Sign-in runs in the desktop app",
        tone: "danger",
      });
      return;
    }
    setConnecting(true);
    const port = 8765;
    try {
      const url = await oauthStart(port, serverUrl);
      await openUrl(url);
      // Poll for the callback token.
      for (let i = 0; i < 120; i++) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const token = await oauthPoll(port);
        if (token) {
          await setSecret("account.sessionToken", token);
          await oauthClear(port);
          pushToast({
            title: "Signed in",
            description: "Google account connected.",
            tone: "success",
          });
          setConnecting(false);
          return;
        }
      }
      pushToast({
        title: "Sign-in timed out",
        tone: "danger",
      });
    } catch (error) {
      pushToast({
        title: "Sign-in failed",
        description: error instanceof Error ? error.message : String(error),
        tone: "danger",
      });
    } finally {
      setConnecting(false);
    }
  };

  return (
    <div className="space-y-7">
      <div className="overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]">
        <div className="flex items-center gap-4 px-5 py-5">
          <div className="flex h-12 w-12 flex-none items-center justify-center rounded-full border border-line bg-[var(--bg-elevated)] text-[16px] font-semibold text-fg">
            G
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <p className="text-[13.5px] font-semibold text-fg">Guest</p>
              <span className="rounded-full border border-line bg-[var(--bg-hover)] px-2 py-0.5 text-[8.5px] font-medium uppercase tracking-wide text-subtle">
                local only
              </span>
            </div>
            <p className="mt-1 text-[10.5px] leading-relaxed text-subtle">
              This workspace is not connected to an account. Everything stays
              on this device.
            </p>
          </div>
        </div>
        <div className="border-t border-line bg-[color-mix(in_srgb,var(--bg-base)_45%,transparent)] px-5 py-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <input
              value={serverUrl}
              onChange={(event) => setServerUrl(event.target.value)}
              placeholder="https://omniilink.dpdns.org"
              className="field h-8 flex-1 text-[11px]"
            />
            <button
              type="button"
              onClick={() => void connectGoogle()}
              disabled={connecting}
              className="btn btn-primary h-8 text-[10.5px]"
            >
              {connecting ? "Connecting…" : "Connect Google"}
            </button>
          </div>
          <p className="mt-2 text-[10px] leading-relaxed text-subtle">
            Opens your browser to sign in. The app listens on 127.0.0.1:8765
            for the callback.
          </p>
        </div>
      </div>

      <section>
        <GroupLabel>Connection</GroupLabel>
        <Group>
          <Row
            title="Cloud sync"
            description="Keep chats, projects, MCP servers, and prompts consistent across devices."
          >
            <span className="flex items-center gap-1.5 text-[10px] text-subtle">
              <span className="h-1.5 w-1.5 rounded-full bg-[var(--fg-subtle)]" />
              Not connected
            </span>
          </Row>
          <Row
            title="Authentication"
            description="Google OAuth with a loopback callback and guest mode."
          >
            <span className="text-[10px] text-subtle">Guest</span>
          </Row>
          <Row
            title="Secrets"
            description="Provider keys will use the operating system keychain."
          >
            <span className="text-[10px] text-subtle">OS keychain</span>
          </Row>
        </Group>
      </section>
    </div>
  );
}

function UpdatesSettings() {
  const autoCheckUpdates = useApp((state) => state.autoCheckUpdates);
  const setAutoCheckUpdates = useApp((state) => state.setAutoCheckUpdates);
  const pushToast = useApp((state) => state.pushToast);
  const [checking, setChecking] = useState(false);

  const checkNow = async () => {
    if (!IS_TAURI) {
      pushToast({
        title: "Updates are checked in the desktop app",
        tone: "danger",
      });
      return;
    }
    setChecking(true);
    try {
      const { check } = await import("@tauri-apps/plugin-updater");
      const update = await check();
      if (update) {
        pushToast({
          title: `Update ${update.version} available`,
          description: "Download and install from the release page.",
          tone: "success",
        });
      } else {
        pushToast({ title: "You are up to date", tone: "success" });
      }
    } catch (error) {
      pushToast({
        title: "Update check failed",
        description: error instanceof Error ? error.message : String(error),
        tone: "danger",
      });
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="space-y-7">
      <div className="flex items-center gap-4 rounded-xl border border-line bg-[var(--bg-surface)] px-5 py-5">
        <div className="flex h-11 w-11 flex-none items-center justify-center rounded-xl border border-line bg-[var(--bg-elevated)] text-accent-text">
          <Download size={19} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="text-[13.5px] font-semibold text-fg">
              OmniLink Desktop
            </p>
            <span className="rounded border border-line px-1.5 py-px font-mono text-[9px] text-subtle">
              v0.1.0
            </span>
          </div>
          <p className="mt-1 text-[10.5px] text-subtle">
            You are on the latest development build.
          </p>
        </div>
        <span className="flex items-center gap-1.5 rounded-full border border-[color-mix(in_srgb,var(--success)_28%,transparent)] bg-[color-mix(in_srgb,var(--success)_9%,transparent)] px-2.5 py-1 text-[9.5px] font-medium text-success">
          <CheckCircle2 size={11} />
          Up to date
        </span>
      </div>

      <section>
        <GroupLabel>Release channel</GroupLabel>
        <Group>
          <Row
            title="Channel"
            description="Signed builds delivered through GitHub Releases."
          >
            <Segmented
              value="stable"
              onChange={() => undefined}
              label="Release channel"
              options={[{ value: "stable", label: "Stable" }]}
            />
          </Row>
          <Row
            title="Check automatically"
            description="Look for updates when the app starts."
          >
            <Switch
              checked={autoCheckUpdates}
              onChange={setAutoCheckUpdates}
              label="Check for updates automatically"
            />
          </Row>
          <Row
            title="Check now"
            description="Queries the signed update feed."
          >
            <button
              type="button"
              onClick={() => void checkNow()}
              disabled={checking}
              className="btn h-7 text-[10.5px]"
            >
              {checking ? "Checking…" : "Check for updates"}
            </button>
          </Row>
        </Group>
      </section>

      <div className="flex items-start gap-3 rounded-xl border border-line bg-[var(--bg-surface)] p-4">
        <Lock size={14} className="mt-0.5 flex-none text-subtle" />
        <p className="text-[10px] leading-relaxed text-subtle">
          Updates install per machine and require administrator approval.
          Artifacts are verified against a signed manifest before the installer
          runs. {autoCheckUpdates ? "Automatic checks are on." : "Automatic checks are off."}
        </p>
      </div>

      <button
        type="button"
        className="btn btn-ghost h-7 text-[10.5px]"
        onClick={() =>
          pushToast({
            title: "Update feed not configured",
            description: "This development build has no release feed yet.",
          })
        }
      >
        <RefreshCw size={11} />
        Recheck release feed
      </button>
    </div>
  );
}

function AboutSettings() {
  const pushToast = useApp((state) => state.pushToast);
  const resolvedTheme = useResolvedTheme();

  const copyDetails = async () => {
    try {
      await copyText(
        JSON.stringify(
          {
            app: "OmniLink Desktop",
            version: "0.1.0",
            identifier: "com.omnilink.desktop",
            shell: "Tauri 2",
            frontend: "React 19",
            theme: resolvedTheme,
            runtime: IS_TAURI ? "tauri" : "browser",
          },
          null,
          2,
        ),
      );
      pushToast({ title: "Build details copied", tone: "success" });
    } catch {
      pushToast({ title: "Could not access the clipboard", tone: "danger" });
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col items-center rounded-xl border border-line bg-[var(--bg-surface)] px-6 py-8 text-center">
        <div className="relative">
          <div className="absolute inset-0 rounded-2xl bg-accent opacity-25 blur-2xl" />
          <div className="relative">
            <span className="flex h-14 w-14 items-center justify-center rounded-2xl border border-line bg-[var(--bg-elevated)] text-[20px] font-semibold text-accent-text">
              ∞
            </span>
          </div>
        </div>
        <h2 className="mt-4 text-[20px] font-semibold tracking-[-0.025em] text-fg">
          OmniLink
        </h2>
        <p className="mt-1 max-w-[380px] text-[11.5px] leading-relaxed text-subtle">
          A local-first desktop workspace for conversations, code, MCP tools,
          and the prompts you reuse.
        </p>
        <div className="mt-4 flex flex-wrap items-center justify-center gap-1.5">
          {["Tauri 2", "React 19", "Tailwind 4", "Local-first"].map((tech) => (
            <span
              key={tech}
              className="rounded-full border border-line bg-[var(--bg-hover)] px-2 py-0.5 text-[9px] font-medium text-subtle"
            >
              {tech}
            </span>
          ))}
        </div>
        <button
          type="button"
          className="btn mt-5 h-7 text-[10.5px]"
          onClick={() => void copyDetails()}
        >
          <Copy size={11} />
          Copy build details
        </button>
      </div>

      <Group>
        <Row title="Version" description="Desktop build">
          <span className="font-mono text-[10.5px] text-muted">0.1.0</span>
        </Row>
        <Row title="Identifier" description="Application bundle identifier">
          <span className="font-mono text-[10.5px] text-muted">
            com.omnilink.desktop
          </span>
        </Row>
        <Row title="Shell" description="Native window and packaging">
          <span className="text-[10.5px] text-muted">Tauri 2</span>
        </Row>
        <Row title="Build channel" description="Where this build came from">
          <span className="text-[10.5px] text-muted">Desktop · unsigned</span>
        </Row>
      </Group>

      <div className="flex items-start gap-3 rounded-xl border border-line bg-[var(--bg-surface)] p-4">
        <ShieldCheck size={14} className="mt-0.5 flex-none text-subtle" />
        <p className="text-[10px] leading-relaxed text-subtle">
          OmniLink runs on Tauri 2 with a Rust backend and a React frontend.
          This prototype is unsigned, so Windows SmartScreen may ask for
          confirmation when installing.
        </p>
      </div>

      <p className="text-center text-[9.5px] text-subtle">
        © 2026 OmniLink · Built for Windows, macOS, and Linux
      </p>
    </div>
  );
}

export function SettingsView() {
  const section = useApp((state) => state.settingsSection);

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[52px] flex-none items-center gap-3 border-b border-line px-6">
        <div className="min-w-0 flex-1">
          <h1 className="text-[13px] font-semibold tracking-[-0.01em] text-fg">
            {sectionTitles[section]}
          </h1>
          <p className="mt-0.5 text-[10px] text-subtle">
            {sectionDescriptions[section]}
          </p>
        </div>
        {section === "updates" && (
          <span className="flex items-center gap-1.5 text-[10px] text-subtle">
            <span className="h-1.5 w-1.5 rounded-full bg-success" />
            v0.1.0
          </span>
        )}
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[820px] px-6 py-6">
          {section === "appearance" && <AppearanceSettings />}
          {section === "models" && <ModelsSettings />}
          {section === "data" && <DataSettings />}
          {section === "account" && <AccountSettings />}
          {section === "updates" && <UpdatesSettings />}
          {section === "about" && <AboutSettings />}

          {section !== "about" && (
            <p className="mt-7 text-center text-[9.5px] text-subtle">
              OmniLink Desktop · local workspace · v0.1.0
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
