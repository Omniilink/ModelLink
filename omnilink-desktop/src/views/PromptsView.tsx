import { Check, Copy, Plus, Search, X } from "lucide-react";
import { useMemo, useState, type MouseEvent } from "react";

import { useApp } from "@/store/app";
import type { Prompt } from "@/types";
import { copyText, formatRelative } from "@/lib/utils";

function PromptCard({ prompt }: { prompt: Prompt }) {
  const openModal = useApp((state) => state.openModal);
  const pushToast = useApp((state) => state.pushToast);
  const [copied, setCopied] = useState(false);

  const copy = async (event: MouseEvent) => {
    event.stopPropagation();
    try {
      await copyText(prompt.body);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
      pushToast({ title: "Prompt copied", tone: "success" });
    } catch {
      pushToast({ title: "Could not access the clipboard", tone: "danger" });
    }
  };

  return (
    <article
      role="button"
      tabIndex={0}
      onClick={() => openModal({ type: "prompt-edit", id: prompt.id })}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openModal({ type: "prompt-edit", id: prompt.id });
        }
      }}
      className="card card-hover group flex cursor-pointer flex-col p-4 outline-none focus-visible:border-[var(--accent-border)]"
    >
      <div className="flex items-start gap-2.5">
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-subtle transition-colors group-hover:text-accent-text">
          <span className="text-[10px] font-semibold">
            {prompt.title.slice(0, 1).toUpperCase()}
          </span>
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-[12.5px] font-medium text-fg">
            {prompt.title}
          </h3>
          <p className="mt-0.5 line-clamp-2 text-[10.5px] leading-relaxed text-subtle">
            {prompt.description || "No description"}
          </p>
        </div>
        <button
          type="button"
          aria-label={`Copy ${prompt.title}`}
          onClick={(event) => void copy(event)}
          className="flex h-6 w-6 flex-none items-center justify-center rounded-md border border-transparent text-subtle transition-colors hover:border-line hover:bg-[var(--bg-hover)] hover:text-fg"
        >
          {copied ? (
            <Check size={12} className="text-success" />
          ) : (
            <Copy size={12} />
          )}
        </button>
      </div>

      <div className="relative mt-3 overflow-hidden rounded-lg border border-line bg-[color-mix(in_srgb,var(--bg-base)_55%,transparent)] px-3 py-2.5">
        <p className="line-clamp-4 whitespace-pre-wrap font-mono text-[10px] leading-[1.65] text-muted">
          {prompt.body}
        </p>
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-[color-mix(in_srgb,var(--bg-base)_85%,transparent)] to-transparent" />
      </div>

      <div className="mt-auto flex items-center gap-1.5 pt-3">
        {prompt.tags.slice(0, 3).map((tag) => (
          <span
            key={tag}
            className="rounded border border-line bg-[var(--bg-hover)] px-1.5 py-px text-[8.5px] font-medium text-subtle"
          >
            {tag}
          </span>
        ))}
        <span className="flex-1" />
        <span className="text-[9.5px] text-subtle">
          {formatRelative(prompt.updatedAt)}
        </span>
      </div>
    </article>
  );
}

export function PromptsView() {
  const prompts = useApp((state) => state.prompts);
  const promptTag = useApp((state) => state.promptTag);
  const setPromptTag = useApp((state) => state.setPromptTag);
  const openModal = useApp((state) => state.openModal);
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    return prompts
      .filter((prompt) => {
        if (promptTag && !prompt.tags.includes(promptTag)) return false;
        if (!term) return true;
        return `${prompt.title} ${prompt.description} ${prompt.body} ${prompt.tags.join(" ")}`
          .toLowerCase()
          .includes(term);
      })
      .sort((a, b) => b.updatedAt - a.updatedAt);
  }, [promptTag, prompts, query]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[52px] flex-none items-center gap-3 border-b border-line px-6">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="text-[13px] font-semibold tracking-[-0.01em] text-fg">
              Prompt library
            </h1>
            <span className="rounded-full bg-[var(--bg-hover)] px-1.5 py-px text-[9px] tabular-nums text-subtle">
              {prompts.length}
            </span>
            {promptTag && (
              <button
                type="button"
                onClick={() => setPromptTag(null)}
                className="flex items-center gap-1 rounded-full border border-[var(--accent-border)] bg-[var(--accent-soft)] px-1.5 py-px text-[9px] font-medium text-accent-text transition-opacity hover:opacity-80"
              >
                {promptTag}
                <X size={9} />
              </button>
            )}
          </div>
          <p className="mt-0.5 text-[10px] text-subtle">
            Reusable instructions, one click away
          </p>
        </div>
        <div className="relative hidden sm:block">
          <Search
            size={12}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-subtle"
          />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search prompts"
            className="h-7 w-[190px] rounded-md border border-line bg-[color-mix(in_srgb,var(--bg-base)_55%,transparent)] pl-7 pr-2 text-[11px] outline-none transition-colors placeholder:text-subtle hover:border-line-strong focus:border-[var(--accent-border)]"
          />
        </div>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => openModal({ type: "prompt-new" })}
        >
          <Plus size={13} />
          New prompt
        </button>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-6 py-5">
        {filtered.length === 0 ? (
          <div className="flex h-full min-h-[320px] flex-col items-center justify-center text-center">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-[var(--bg-elevated)] text-subtle">
              <Search size={18} />
            </div>
            <p className="mt-3 text-[13px] font-medium text-fg">
              No prompts match
            </p>
            <p className="mt-1 max-w-[280px] text-[11px] leading-relaxed text-subtle">
              {query || promptTag
                ? "Try a different search or clear the active filter."
                : "Save your first reusable instruction to get started."}
            </p>
            <div className="mt-4 flex gap-2">
              {(query || promptTag) && (
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setQuery("");
                    setPromptTag(null);
                  }}
                >
                  Clear filters
                </button>
              )}
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => openModal({ type: "prompt-new" })}
              >
                <Plus size={13} />
                New prompt
              </button>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {filtered.map((prompt) => (
              <PromptCard key={prompt.id} prompt={prompt} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
