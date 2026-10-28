import {
  isModelConfigured,
  listOllamaModels,
  pickOllamaModel,
  resolveAuthSource,
} from "@/lib/models";
import { useApp } from "@/store/app";

const DEFAULT_OLLAMA = "http://127.0.0.1:11434";

/** Add a scheme to hand-typed hosts like `example.com/v1`. */
export function normalizeBaseUrl(url: string): string {
  const trimmed = url.trim();
  if (!trimmed) return "";
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) return trimmed;
  if (/^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i.test(trimmed)) {
    return `http://${trimmed}`;
  }
  return `https://${trimmed}`;
}

/**
 * Make sure the app can actually talk to a model on launch.
 *
 * Saved settings can drift: a hand-typed host without a scheme, a provider
 * whose key is gone, or a base URL that no longer answers. Left alone, the
 * composer just refuses to send and the app looks broken. When the current
 * configuration cannot work, fall back to a local Ollama instance if one is
 * running — the one provider that needs no credentials.
 */
export async function ensureUsableModelConfig(): Promise<void> {
  const state = useApp.getState();
  const ollamaUrl = normalizeBaseUrl(state.ollamaUrl) || DEFAULT_OLLAMA;

  const normalizedBase = normalizeBaseUrl(state.modelBaseUrl);
  if (normalizedBase && normalizedBase !== state.modelBaseUrl) {
    state.setModelBaseUrl(normalizedBase);
  }

  const current = {
    provider: state.modelProvider,
    baseUrl: normalizedBase,
    apiKey: state.modelApiKey,
    model: state.modelName,
    authSource: resolveAuthSource(normalizedBase),
  };

  if (isModelConfigured(current)) return;

  try {
    const entries = await listOllamaModels(ollamaUrl);
    if (entries.length === 0) return;

    const next = useApp.getState();
    next.setModelProvider("ollama");
    next.setModelBaseUrl(ollamaUrl);
    next.setModelName(pickOllamaModel(entries));
    next.setModelApiKey("");
    next.setActiveProvider(null);
    next.pushToast({
      title: "Using local Ollama",
      description: `The saved connection needs a key, so OmniLink switched to ${pickOllamaModel(entries)}.`,
      tone: "success",
    });
  } catch {
    // No local Ollama either; leave the settings untouched so the user can
    // fix them in Settings → Models.
  }
}
