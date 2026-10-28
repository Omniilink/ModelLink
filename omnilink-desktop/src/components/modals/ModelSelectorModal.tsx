import { Check, Cpu, Loader2, RefreshCw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Dialog } from "@/components/ui/Dialog";
import { useApp } from "@/store/app";
import { cn } from "@/lib/utils";
import {
  isOpenCodeEndpoint,
  listOllamaModels,
  listModels,
  pickOllamaModel,
} from "@/lib/models";
import { opencodeAuthStatus } from "@/lib/tauri";

const OPENCODE_GO_BASE = "https://opencode.ai/inference/go/openai/v1";
const OPENCODE_ZEN_BASE = "https://opencode.ai/inference/openai/v1";

export function ModelSelectorModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const modelProvider = useApp((state) => state.modelProvider);
  const modelName = useApp((state) => state.modelName);
  const setModelProvider = useApp((state) => state.setModelProvider);
  const setModelName = useApp((state) => state.setModelName);
  const setModelBaseUrl = useApp((state) => state.setModelBaseUrl);
  const setModelApiKey = useApp((state) => state.setModelApiKey);
  const setActiveProvider = useApp((state) => state.setActiveProvider);
  const providers = useApp((state) => state.providers);
  const activeProviderId = useApp((state) => state.activeProviderId);
  const ollamaUrl = useApp((state) => state.ollamaUrl);
  const modelBaseUrl = useApp((state) => state.modelBaseUrl);

  const [ollamaModels, setOllamaModels] = useState<
    { name: string; size: number }[]
  >([]);
  const [loadingOllama, setLoadingOllama] = useState(false);
  const [ollamaError, setOllamaError] = useState<string | null>(null);

  const [zenModels, setZenModels] = useState<string[]>([]);
  const [goModels, setGoModels] = useState<string[]>([]);
  const [authSources, setAuthSources] = useState<string[]>([]);
  const [loadingCloud, setLoadingCloud] = useState(false);
  const [cloudError, setCloudError] = useState<string | null>(null);

  const loadOllama = useCallback(async () => {
    setLoadingOllama(true);
    setOllamaError(null);
    try {
      const models = await listOllamaModels(ollamaUrl);
      setOllamaModels(
        models.map((entry) => ({ name: entry.name, size: entry.size })),
      );
    } catch (error) {
      setOllamaModels([]);
      setOllamaError(
        error instanceof Error ? error.message : "Ollama is not reachable.",
      );
    } finally {
      setLoadingOllama(false);
    }
  }, [ollamaUrl]);

  const loadCloud = useCallback(async () => {
    setLoadingCloud(true);
    setCloudError(null);
    try {
      const sources = await opencodeAuthStatus();
      setAuthSources(sources);

      const base = {
        provider: "custom" as const,
        apiKey: "",
        model: "",
      };

      const results = await Promise.allSettled([
        listModels({
          ...base,
          baseUrl: OPENCODE_ZEN_BASE,
          authSource: "opencode",
        }),
        listModels({
          ...base,
          baseUrl: OPENCODE_GO_BASE,
          authSource: "opencode-go",
        }),
      ]);

      if (results[0].status === "fulfilled") setZenModels(results[0].value);
      if (results[1].status === "fulfilled") setGoModels(results[1].value);

      if (
        results[0].status === "rejected" &&
        results[1].status === "rejected"
      ) {
        setCloudError(
          "Could not reach OpenCode. Check your connection or sign in with the OpenCode CLI.",
        );
      }
    } catch (error) {
      setCloudError(
        error instanceof Error ? error.message : "Could not reach OpenCode.",
      );
    } finally {
      setLoadingCloud(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void loadOllama();
    void loadCloud();
  }, [open, loadOllama, loadCloud]);

  const selectLocal = (model: string, baseUrl: string) => {
    setModelProvider("ollama");
    setModelName(model);
    setModelBaseUrl(baseUrl);
    setModelApiKey("");
    setActiveProvider(null);
    onClose();
  };

  const selectCloud = (
    model: string,
    baseUrl: string,
    authSource: string,
  ) => {
    setModelProvider("custom");
    setModelName(model);
    setModelBaseUrl(baseUrl);
    setModelApiKey("");
    setActiveProvider(null);
    // authSource is derived from the base URL at request time.
    void authSource;
    onClose();
  };

  const currentIsLocal =
    modelProvider === "ollama" && !isOpenCodeEndpoint(modelBaseUrl);

  const suggested = pickOllamaModel(ollamaModels);

  return (
    <Dialog open={open} onClose={onClose} title="Select model">
      <div className="max-h-[65vh] space-y-5 overflow-auto p-4">
        <section>
          <div className="mb-2 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Cpu size={12} className="text-muted" />
              <span className="text-[11px] font-medium text-fg">
                Ollama (local)
              </span>
              <span className="text-[9px] text-subtle">{ollamaUrl}</span>
            </div>
            <button
              type="button"
              onClick={() => void loadOllama()}
              className="flex items-center gap-1 text-[9.5px] text-subtle hover:text-fg"
            >
              <RefreshCw
                size={10}
                className={loadingOllama ? "animate-spin" : undefined}
              />
              Refresh
            </button>
          </div>

          {loadingOllama && ollamaModels.length === 0 && (
            <p className="flex items-center gap-1.5 px-1 py-2 text-[10px] text-subtle">
              <Loader2 size={11} className="animate-spin" />
              Looking for local models…
            </p>
          )}

          {ollamaError && (
            <p className="rounded-lg border border-line bg-[var(--bg-base)] px-2.5 py-2 text-[10px] leading-relaxed text-danger">
              {ollamaError}
            </p>
          )}

          {!loadingOllama && !ollamaError && ollamaModels.length === 0 && (
            <p className="px-1 py-2 text-[10px] text-subtle">
              No models found. Run <code className="font-mono">ollama pull llama3.2</code>.
            </p>
          )}

          <div className="space-y-1">
            {ollamaModels.map((entry) => {
              const model = entry.name;
              const active = currentIsLocal && modelName === model;
              return (
                <button
                  key={model}
                  type="button"
                  onClick={() => selectLocal(model, ollamaUrl)}
                  className={cn(
                    "flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left text-[11px] transition-colors hover:bg-[var(--bg-hover)]",
                    active && "bg-[var(--bg-active)]",
                  )}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="truncate font-mono text-fg">{model}</span>
                    {model === suggested && ollamaModels.length > 1 && (
                      <span className="rounded border border-line px-1 py-px text-[8px] uppercase tracking-wide text-subtle">
                        fastest
                      </span>
                    )}
                  </span>
                  {active && <Check size={12} className="text-accent-text" />}
                </button>
              );
            })}
          </div>
        </section>

        <section>
          <div className="mb-2 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Sparkles size={12} className="text-muted" />
              <span className="text-[11px] font-medium text-fg">
                OpenCode
              </span>
              {authSources.length > 0 ? (
                <span className="rounded border border-[var(--accent-border)] bg-[var(--accent-soft)] px-1.5 py-px text-[8px] font-semibold uppercase tracking-wide text-accent-text">
                  {authSources.includes("opencode-go") ? "Go ready" : "Zen ready"}
                </span>
              ) : (
                <span className="text-[9px] text-subtle">not signed in</span>
              )}
            </div>
            <button
              type="button"
              onClick={() => void loadCloud()}
              className="flex items-center gap-1 text-[9.5px] text-subtle hover:text-fg"
            >
              <RefreshCw
                size={10}
                className={loadingCloud ? "animate-spin" : undefined}
              />
              Refresh
            </button>
          </div>

          {cloudError && (
            <p className="rounded-lg border border-line bg-[var(--bg-base)] px-2.5 py-2 text-[10px] leading-relaxed text-danger">
              {cloudError}
            </p>
          )}

          {!cloudError && authSources.length === 0 && !loadingCloud && (
            <p className="px-1 py-1 text-[10px] leading-relaxed text-subtle">
              Sign in with the OpenCode CLI once and every model below becomes
              available here automatically.
            </p>
          )}

          {!cloudError && authSources.length > 0 && !authSources.includes("opencode") && (
            <p className="px-1 py-1 text-[10px] leading-relaxed text-subtle">
              Zen models need an OpenCode console API key. Add one under
              Settings → Models, or use a Go subscription.
            </p>
          )}

          {goModels.length > 0 && (
            <div className="mt-2">
              <p className="mb-1 px-1 text-[9.5px] uppercase tracking-wide text-subtle">
                Go subscription
              </p>
              <div className="space-y-1">
                {goModels.map((model) => {
                  const active =
                    modelName === model && modelBaseUrl === OPENCODE_GO_BASE;
                  return (
                    <button
                      key={`go-${model}`}
                      type="button"
                      onClick={() =>
                        selectCloud(model, OPENCODE_GO_BASE, "opencode-go")
                      }
                      className={cn(
                        "flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left text-[11px] transition-colors hover:bg-[var(--bg-hover)]",
                        active && "bg-[var(--bg-active)]",
                      )}
                    >
                      <span className="truncate font-mono text-fg">{model}</span>
                      {active && <Check size={12} className="text-accent-text" />}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {zenModels.length > 0 && (
            <div className="mt-3">
              <p className="mb-1 px-1 text-[9.5px] uppercase tracking-wide text-subtle">
                Zen
              </p>
              <div className="space-y-1">
                {zenModels.map((model) => {
                  const active =
                    modelName === model && modelBaseUrl === OPENCODE_ZEN_BASE;
                  return (
                    <button
                      key={`zen-${model}`}
                      type="button"
                      onClick={() =>
                        selectCloud(model, OPENCODE_ZEN_BASE, "opencode")
                      }
                      className={cn(
                        "flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left text-[11px] transition-colors hover:bg-[var(--bg-hover)]",
                        active && "bg-[var(--bg-active)]",
                      )}
                    >
                      <span className="truncate font-mono text-fg">{model}</span>
                      {active && <Check size={12} className="text-accent-text" />}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </section>

        {providers.length > 0 && (
          <section>
            <div className="mb-2 flex items-center gap-2">
              <Sparkles size={12} className="text-muted" />
              <span className="text-[11px] font-medium text-fg">
                Saved providers
              </span>
            </div>
            <div className="space-y-1">
              {providers.map((provider) => (
                <button
                  key={provider.id}
                  type="button"
                  onClick={() => {
                    setModelProvider("custom");
                    setModelName(provider.model);
                    setModelBaseUrl(provider.baseUrl);
                    setActiveProvider(provider.id);
                    onClose();
                  }}
                  className={cn(
                    "flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left text-[11px] transition-colors hover:bg-[var(--bg-hover)]",
                    activeProviderId === provider.id && "bg-[var(--bg-active)]",
                  )}
                >
                  <div className="min-w-0">
                    <p className="truncate text-fg">{provider.name}</p>
                    <p className="truncate font-mono text-[9px] text-subtle">
                      {provider.model}
                    </p>
                  </div>
                  {activeProviderId === provider.id && (
                    <Check size={12} className="text-accent-text" />
                  )}
                </button>
              ))}
            </div>
          </section>
        )}
      </div>
    </Dialog>
  );
}
