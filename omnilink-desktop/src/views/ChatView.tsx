import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowUp,
  Check,
  Code2,
  Copy,
  Cpu,
  GitBranch,
  Map,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { IconButton } from "@/components/ui/IconButton";
import { Logo } from "@/components/ui/Logo";
import { useApp } from "@/store/app";
import type { Message, ModelProvider } from "@/types";
import { cn, copyText, formatTime } from "@/lib/utils";
import { IS_TAURI, getSecret } from "@/lib/tauri";
import { isModelConfigured, streamChat } from "@/lib/models";

const suggestions = [
  {
    icon: Code2,
    title: "Explain this codebase",
    hint: "Map the important files and flows",
    prompt: "Explain this codebase to a new contributor. Start with the runtime shape, then walk through the three most important flows.",
  },
  {
    icon: GitBranch,
    title: "Draft a commit message",
    hint: "Subject, body, and reasoning",
    prompt: "Write a commit message for my staged changes. Use a conventional commit subject and explain the why in the body.",
  },
  {
    icon: Map,
    title: "Plan a feature",
    hint: "Scope, edge cases, build order",
    prompt: "Turn this feature idea into an implementation plan with scope, data flow, edge cases, and a verifiable build order.",
  },
  {
    icon: Search,
    title: "Review my changes",
    hint: "Catch correctness and security issues",
    prompt: "Review my recent changes like a senior engineer. Prioritize correctness, security, and missing tests. Give concrete fixes.",
  },
];

function MessageRow({ message }: { message: Message }) {
  const [copied, setCopied] = useState(false);
  const isUser = message.role === "user";

  const copy = async () => {
    try {
      await copyText(message.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard access can fail in hardened webviews; the text stays visible.
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
      className={cn(
        "group relative flex gap-3 py-3.5",
        isUser && "flex-row-reverse",
      )}
    >
      {isUser ? (
        <div className="mt-6 flex h-6 w-6 flex-none items-center justify-center rounded-full border border-line bg-[var(--bg-elevated)] text-[9.5px] font-semibold text-muted">
          G
        </div>
      ) : (
        <Logo size={24} className="mt-6" />
      )}
      <div className={cn("min-w-0 max-w-[86%]", isUser && "flex flex-col items-end")}>
        <div className="mb-1 flex items-center gap-2 px-0.5">
          <span className="text-[10.5px] font-medium text-muted">
            {isUser ? "You" : "OmniLink"}
          </span>
          <span className="text-[9.5px] text-subtle">
            {formatTime(message.createdAt)}
          </span>
          <button
            type="button"
            aria-label="Copy message"
            onClick={copy}
            className="flex h-4 w-4 items-center justify-center rounded text-subtle opacity-0 transition-opacity hover:text-fg group-hover:opacity-100 focus-visible:opacity-100"
          >
            {copied ? (
              <Check size={11} className="text-success" />
            ) : (
              <Copy size={11} />
            )}
          </button>
        </div>
        {isUser ? (
          <div className="rounded-2xl rounded-tr-md border border-line bg-[var(--bg-elevated)] px-3.5 py-2.5 text-[13px] leading-relaxed text-fg shadow-[0_1px_2px_rgba(0,0,0,0.12)]">
            <p className="whitespace-pre-wrap break-words">{message.content}</p>
          </div>
        ) : (
          <div className="prose-chat">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </motion.div>
  );
}

function Composer({
  conversationId,
  draft,
  onDraftChange,
  noModel,
  streaming,
  onSend,
}: {
  conversationId: string;
  draft: string;
  onDraftChange: (value: string) => void;
  noModel: boolean;
  streaming: boolean;
  onSend: (content: string) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const openSettings = useApp((state) => state.openSettings);
  const pushToast = useApp((state) => state.pushToast);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  }, [draft, conversationId]);

  const submit = () => {
    if (!draft.trim() || streaming) return;
    if (noModel) {
      pushToast({
        title: "Connect a model first",
        description:
          "Open Settings → Models to connect OpenCode Zen or a local Ollama endpoint.",
      });
      return;
    }
    onSend(draft.trim());
    onDraftChange("");
  };

  return (
    <div className="w-full">
      {noModel && (
        <div className="mb-2 flex items-center gap-2 rounded-lg border border-[var(--accent-border)] bg-[var(--accent-softer)] px-3 py-1.5 text-[10.5px]">
          <Sparkles size={12} className="flex-none text-accent-text" />
          <span className="min-w-0 flex-1 truncate text-muted">
            Connect a model to turn drafts into replies.
          </span>
          <button
            type="button"
            onClick={() => openSettings("models")}
            className="flex-none font-medium text-accent-text transition-opacity hover:opacity-80"
          >
            Set up models
          </button>
        </div>
      )}
      <div className="overflow-hidden rounded-2xl border border-line bg-[color-mix(in_srgb,var(--bg-elevated)_92%,transparent)] shadow-[0_10px_40px_-18px_rgba(0,0,0,0.5)] transition-colors focus-within:border-[var(--accent-border)]">
        <textarea
          ref={textareaRef}
          data-composer
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          rows={1}
          placeholder="Ask anything, or describe what you want to build…"
          className="max-h-[200px] min-h-[52px] w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-[13px] leading-relaxed text-fg outline-none placeholder:text-subtle"
        />
        <div className="flex items-center gap-1.5 px-2.5 pb-2.5 pt-1">
          <button
            type="button"
            onClick={() => openSettings("models")}
            className="flex h-6 items-center gap-1.5 rounded-md border border-transparent px-1.5 text-[10.5px] text-subtle transition-colors hover:border-line hover:bg-[var(--bg-hover)] hover:text-muted"
          >
            <Cpu size={11} />
            {noModel ? "No model" : "Model"}
          </button>
          <span className="dot bg-[var(--fg-subtle)] opacity-60" />
          <span className="text-[10px] text-subtle">Local draft</span>
          <div className="flex-1" />
          <span className="mr-1 hidden text-[9.5px] text-subtle sm:block">
            Enter to send · Shift+Enter for a new line
          </span>
          <button
            type="button"
            aria-label="Send message"
            onClick={submit}
            disabled={!draft.trim() || streaming}
            className={cn(
              "flex h-7 w-7 flex-none items-center justify-center rounded-lg transition-all",
              draft.trim()
                ? "bg-accent text-accent-fg shadow-[0_1px_2px_rgba(0,0,0,0.2)] hover:bg-[var(--accent-hover)]"
                : "bg-[var(--bg-active)] text-subtle",
            )}
          >
            <ArrowUp size={15} strokeWidth={2} />
          </button>
        </div>
      </div>
    </div>
  );
}

export function ChatView() {
  const conversations = useApp((state) => state.conversations);
  const activeId = useApp((state) => state.activeConversationId);
  const deleteConversation = useApp((state) => state.deleteConversation);
  const newConversation = useApp((state) => state.newConversation);
  const pushToast = useApp((state) => state.pushToast);
  const appendMessage = useApp((state) => state.appendMessage);
  const updateMessage = useApp((state) => state.updateMessage);
  const modelProvider = useApp((state) => state.modelProvider);
  const modelBaseUrl = useApp((state) => state.modelBaseUrl);
  const modelApiKey = useApp((state) => state.modelApiKey);
  const modelName = useApp((state) => state.modelName);
  const [draft, setDraft] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [copiedTranscript, setCopiedTranscript] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const conversation = conversations.find((item) => item.id === activeId);
  const providers = useApp((state) => state.providers);
  const activeProviderId = useApp((state) => state.activeProviderId);
  const activeProvider = providers.find((p) => p.id === activeProviderId);
  const [providerKey, setProviderKey] = useState("");
  useEffect(() => {
    if (!activeProvider) {
      setProviderKey("");
      return;
    }
    if (!activeProvider.hasKey) {
      setProviderKey("");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const v = IS_TAURI
          ? await getSecret(`provider.${activeProvider.id}.apiKey`)
          : window.localStorage.getItem(`provider.${activeProvider.id}.apiKey`);
        if (!cancelled) setProviderKey(v ?? "");
      } catch {
        if (!cancelled) setProviderKey("");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeProvider?.id, activeProvider?.hasKey]);

  const modelConfig = activeProvider
    ? {
        provider: (activeProvider.baseUrl.includes("127.0.0.1") ||
        activeProvider.baseUrl.includes("localhost")
          ? "ollama"
          : "custom") as ModelProvider,
        baseUrl: activeProvider.baseUrl,
        apiKey: providerKey,
        model: activeProvider.model,
      }
    : {
        provider: modelProvider,
        baseUrl: modelBaseUrl,
        apiKey: modelApiKey,
        model: modelName,
      };
  const noModel = !isModelConfigured(modelConfig);

  const handleSend = async (content: string) => {
    if (!conversation || streaming) return;
    const userMessage: Message = {
      id: `message-${Date.now()}`,
      role: "user",
      content,
      createdAt: Date.now(),
    };
    appendMessage(conversation.id, userMessage);
    const assistantId = `message-${Date.now() + 1}`;
    appendMessage(conversation.id, {
      id: assistantId,
      role: "assistant",
      content: "",
      createdAt: Date.now(),
    });
    setStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let accumulated = "";
    try {
      await streamChat(
        modelConfig,
        [...conversation.messages, userMessage],
        (token) => {
          accumulated += token;
          updateMessage(conversation.id, assistantId, accumulated);
        },
        controller.signal,
      );
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Model request failed";
      updateMessage(
        conversation.id,
        assistantId,
        accumulated || `⚠ ${message}`,
      );
      pushToast({ title: "Model request failed", description: message, tone: "danger" });
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  useEffect(() => {
    setDraft("");
  }, [activeId]);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const frame = window.requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [conversation?.id, conversation?.messages.length]);

  const copyTranscript = async () => {
    if (!conversation) return;
    const transcript = conversation.messages
      .map(
        (message) =>
          `${message.role === "assistant" ? "OmniLink" : "You"}\n${message.content}`,
      )
      .join("\n\n");
    try {
      await copyText(transcript);
      setCopiedTranscript(true);
      window.setTimeout(() => setCopiedTranscript(false), 1500);
      pushToast({ title: "Conversation copied", tone: "success" });
    } catch {
      pushToast({ title: "Could not copy the conversation", tone: "danger" });
    }
  };

  if (!conversation) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-8 text-center">
        <Logo size={48} glow />
        <h1 className="mt-5 text-[22px] font-semibold tracking-[-0.025em] text-fg">
          Your workspace is quiet
        </h1>
        <p className="mt-2 max-w-sm text-[12.5px] leading-relaxed text-muted">
          Start a conversation to think through code, plan a feature, or draft
          something worth keeping.
        </p>
        <button
          type="button"
          className="btn btn-primary mt-5"
          onClick={() => newConversation()}
        >
          Start a new chat
        </button>
      </div>
    );
  }

  const isEmpty = conversation.messages.length === 0;

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[52px] flex-none items-center gap-3 border-b border-line px-5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-[13px] font-semibold tracking-[-0.01em] text-fg">
              {conversation.title}
            </h1>
            {isEmpty && (
              <span className="rounded border border-line px-1.5 py-px text-[8.5px] font-medium uppercase tracking-wide text-subtle">
                draft
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[10px] text-subtle">
            {conversation.messages.length}{" "}
            {conversation.messages.length === 1 ? "message" : "messages"} ·
            stored locally
          </p>
        </div>
        <IconButton
          label={copiedTranscript ? "Copied" : "Copy conversation"}
          onClick={() => void copyTranscript()}
          disabled={isEmpty}
        >
          {copiedTranscript ? (
            <Check size={14} className="text-success" />
          ) : (
            <Copy size={14} />
          )}
        </IconButton>
        <IconButton
          label="Delete conversation"
          onClick={() => setConfirmDelete(true)}
        >
          <Trash2 size={14} />
        </IconButton>
      </div>

      <div ref={scrollRef} className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {isEmpty ? (
          <div className="mx-auto flex min-h-full w-full max-w-[780px] flex-col items-center justify-center px-8 py-10">
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
              className="flex flex-col items-center text-center"
            >
              <Logo size={46} glow />
              <h2 className="mt-5 text-[25px] font-semibold tracking-[-0.03em] text-fg">
                What are we building?
              </h2>
              <p className="mt-2 max-w-[420px] text-[12.5px] leading-relaxed text-muted">
                Ask a question, map an unfamiliar codebase, or shape a rough
                idea into a plan.
              </p>
            </motion.div>

            <div className="mt-7 grid w-full max-w-[620px] grid-cols-1 gap-2 sm:grid-cols-2">
              {suggestions.map((suggestion, index) => {
                const Icon = suggestion.icon;
                return (
                  <motion.button
                    key={suggestion.title}
                    type="button"
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{
                      delay: 0.05 + index * 0.04,
                      duration: 0.24,
                      ease: [0.22, 1, 0.36, 1],
                    }}
                    onClick={() => {
                      setDraft(suggestion.prompt);
                      window.requestAnimationFrame(() => {
                        document
                          .querySelector<HTMLTextAreaElement>(
                            "textarea[data-composer]",
                          )
                          ?.focus();
                      });
                    }}
                    className="card card-hover group flex items-start gap-3 p-3 text-left"
                  >
                    <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-subtle transition-colors group-hover:text-accent-text">
                      <Icon size={13} />
                    </span>
                    <span className="min-w-0">
                      <span className="block text-[11.5px] font-medium text-fg">
                        {suggestion.title}
                      </span>
                      <span className="mt-0.5 block text-[10px] leading-relaxed text-subtle">
                        {suggestion.hint}
                      </span>
                    </span>
                  </motion.button>
                );
              })}
            </div>
          </div>
        ) : (
          <div className="mx-auto w-full max-w-[780px] px-7 pb-4 pt-2">
            <AnimatePresence initial={false}>
              {conversation.messages.map((message) => (
                <MessageRow key={message.id} message={message} />
              ))}
            </AnimatePresence>
          </div>
        )}
      </div>

      <div className="flex-none px-5 pb-4 pt-2">
        <div className="mx-auto w-full max-w-[780px]">
          <Composer
            conversationId={conversation.id}
            draft={draft}
            onDraftChange={setDraft}
            noModel={noModel}
            streaming={streaming}
            onSend={(content) => void handleSend(content)}
          />
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete conversation?"
        description={`"${conversation.title}" and its messages will be removed from this device.`}
        confirmLabel="Delete chat"
        onClose={() => setConfirmDelete(false)}
        onConfirm={() => {
          deleteConversation(conversation.id);
          pushToast({ title: "Conversation deleted" });
        }}
      />
    </div>
  );
}
