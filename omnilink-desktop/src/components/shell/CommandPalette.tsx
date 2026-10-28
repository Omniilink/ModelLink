import { AnimatePresence, motion } from "framer-motion";
import {
  Blocks,
  BookText,
  Check,
  Code2,
  Cpu,
  Database,
  DownloadCloud,
  FilePlus2,
  FolderPlus,
  Info,
  MessageSquare,
  Monitor,
  Moon,
  Palette,
  Search,
  Server,
  Settings2,
  Sun,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";

import { Kbd, modKey } from "@/components/ui/Kbd";
import { cn } from "@/lib/utils";
import { useApp } from "@/store/app";
import type { Accent } from "@/types";

interface PaletteCommand {
  id: string;
  label: string;
  group: string;
  hint?: string;
  keywords?: string;
  icon?: LucideIcon;
  swatch?: string;
  shortcut?: string;
  checked?: boolean;
  run: () => void;
}

const accentSwatches: Record<Accent, string> = {
  violet: "#8b5cf6",
  cyan: "#06b6d4",
  emerald: "#10b981",
  rose: "#f43f5e",
  amber: "#f59e0b",
};

export function CommandPalette() {
  const open = useApp((state) => state.paletteOpen);
  const setPaletteOpen = useApp((state) => state.setPaletteOpen);
  const section = useApp((state) => state.section);
  const theme = useApp((state) => state.theme);
  const accent = useApp((state) => state.accent);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const commands = useMemo<PaletteCommand[]>(() => {
    const state = useApp.getState();
    const list: PaletteCommand[] = [
      {
        id: "nav-chat",
        label: "Go to Chat",
        group: "Jump to",
        icon: MessageSquare,
        shortcut: `${modKey} 1`,
        checked: section === "chat",
        run: () => state.setSection("chat"),
      },
      {
        id: "nav-coding",
        label: "Go to Coding",
        group: "Jump to",
        icon: Code2,
        shortcut: `${modKey} 2`,
        checked: section === "coding",
        run: () => state.setSection("coding"),
      },
      {
        id: "nav-mcp",
        label: "Go to MCP",
        group: "Jump to",
        icon: Blocks,
        shortcut: `${modKey} 3`,
        checked: section === "mcp",
        run: () => state.setSection("mcp"),
      },
      {
        id: "nav-prompts",
        label: "Go to Prompts",
        group: "Jump to",
        icon: BookText,
        shortcut: `${modKey} 4`,
        checked: section === "prompts",
        run: () => state.setSection("prompts"),
      },
      {
        id: "nav-settings",
        label: "Go to Settings",
        group: "Jump to",
        icon: Settings2,
        shortcut: `${modKey} ,`,
        checked: section === "settings",
        run: () => state.openSettings(state.settingsSection),
      },
      {
        id: "new-chat",
        label: "New chat",
        group: "Create",
        icon: MessageSquare,
        shortcut: `${modKey} N`,
        keywords: "conversation message",
        run: () => state.newConversation(),
      },
      {
        id: "new-project",
        label: "New project",
        group: "Create",
        icon: FolderPlus,
        keywords: "coding files workspace",
        run: () => {
          state.setSection("coding");
          state.openModal({ type: "project-new" });
        },
      },
      {
        id: "new-prompt",
        label: "New prompt",
        group: "Create",
        icon: FilePlus2,
        keywords: "library instruction template",
        run: () => {
          state.setSection("prompts");
          state.openModal({ type: "prompt-new" });
        },
      },
      {
        id: "new-mcp",
        label: "Add MCP server",
        group: "Create",
        icon: Server,
        keywords: "stdio sse http tools",
        run: () => {
          state.setSection("mcp");
          state.openModal({ type: "mcp-new" });
        },
      },
      {
        id: "theme-dark",
        label: "Use dark theme",
        group: "Appearance",
        icon: Moon,
        checked: theme === "dark",
        run: () => state.setTheme("dark"),
      },
      {
        id: "theme-light",
        label: "Use light theme",
        group: "Appearance",
        icon: Sun,
        checked: theme === "light",
        run: () => state.setTheme("light"),
      },
      {
        id: "theme-system",
        label: "Follow system theme",
        group: "Appearance",
        icon: Monitor,
        checked: theme === "system",
        run: () => state.setTheme("system"),
      },
      ...(
        [
          ["violet", "Violet"],
          ["cyan", "Cyan"],
          ["emerald", "Emerald"],
          ["rose", "Rose"],
          ["amber", "Amber"],
        ] as [Accent, string][]
      ).map(([value, label]) => ({
        id: `accent-${value}`,
        label: `Accent: ${label}`,
        group: "Appearance",
        swatch: accentSwatches[value],
        checked: accent === value,
        run: () => state.setAccent(value),
      })),
      {
        id: "settings-appearance",
        label: "Appearance settings",
        group: "Settings",
        icon: Palette,
        keywords: "theme accent motion",
        run: () => state.openSettings("appearance"),
      },
      {
        id: "settings-models",
        label: "Model connections",
        group: "Settings",
        icon: Cpu,
        keywords: "opencode zen ollama provider",
        run: () => state.openSettings("models"),
      },
      {
        id: "settings-data",
        label: "Data & storage",
        group: "Settings",
        icon: Database,
        keywords: "export reset local",
        run: () => state.openSettings("data"),
      },
      {
        id: "settings-account",
        label: "Account",
        group: "Settings",
        icon: UserRound,
        keywords: "sync google guest",
        run: () => state.openSettings("account"),
      },
      {
        id: "settings-updates",
        label: "Updates",
        group: "Settings",
        icon: DownloadCloud,
        keywords: "version release",
        run: () => state.openSettings("updates"),
      },
      {
        id: "settings-about",
        label: "About OmniLink",
        group: "Settings",
        icon: Info,
        keywords: "version build license",
        run: () => state.openSettings("about"),
      },
    ];

    const term = query.trim().toLowerCase();
    if (!term) return list;
    return list.filter((command) =>
      `${command.label} ${command.group} ${command.hint ?? ""} ${command.keywords ?? ""}`
        .toLowerCase()
        .includes(term),
    );
  }, [accent, query, section, theme]);

  const grouped = useMemo(() => {
    const groups: { label: string; items: PaletteCommand[] }[] = [];
    for (const command of commands) {
      const last = groups.at(-1);
      if (last && last.label === command.group) {
        last.items.push(command);
      } else {
        groups.push({ label: command.group, items: [command] });
      }
    }
    return groups;
  }, [commands]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  useEffect(() => {
    if (!open) {
      setQuery("");
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      inputRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const node = document.querySelector<HTMLElement>(
      `[data-palette-index="${activeIndex}"]`,
    );
    node?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open, query]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, setPaletteOpen]);

  const runAt = (index: number) => {
    const command = commands[index];
    if (!command) return;
    command.run();
    setPaletteOpen(false);
    setQuery("");
  };

  const onInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) =>
        commands.length === 0 ? 0 : (index + 1) % commands.length,
      );
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) =>
        commands.length === 0
          ? 0
          : (index - 1 + commands.length) % commands.length,
      );
    } else if (event.key === "Enter") {
      event.preventDefault();
      runAt(activeIndex);
    }
  };

  if (typeof document === "undefined") return null;

  let flatIndex = -1;

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[110] flex items-start justify-center px-6 pt-[11vh]"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.13 }}
        >
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-[3px]"
            onClick={() => setPaletteOpen(false)}
            aria-hidden
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Command palette"
            initial={{ opacity: 0, y: -8, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.99 }}
            transition={{ duration: 0.17, ease: [0.22, 1, 0.36, 1] }}
            className="relative w-full max-w-[600px] overflow-hidden rounded-2xl border border-line-strong bg-[color-mix(in_srgb,var(--bg-overlay)_94%,transparent)] shadow-[0_40px_120px_-20px_rgba(0,0,0,0.7)] backdrop-blur-2xl"
          >
            <div className="flex h-[52px] items-center gap-3 border-b border-line px-4">
              <Search size={16} className="flex-none text-subtle" />
              <input
                ref={inputRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={onInputKeyDown}
                placeholder="Search commands, views, and settings…"
                className="h-full flex-1 bg-transparent text-[13.5px] text-fg outline-none placeholder:text-subtle"
                spellCheck={false}
              />
              <Kbd>Esc</Kbd>
            </div>

            <div className="scroll-thin max-h-[min(58vh,520px)] overflow-y-auto p-2">
              {grouped.length === 0 && (
                <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
                  <div className="mb-3 flex h-9 w-9 items-center justify-center rounded-full border border-line bg-[var(--bg-hover)] text-subtle">
                    <Search size={15} />
                  </div>
                  <p className="text-[12.5px] font-medium text-fg">
                    No commands found
                  </p>
                  <p className="mt-1 text-[11px] text-subtle">
                    Try a view name, "theme", or "new".
                  </p>
                </div>
              )}
              {grouped.map((group) => (
                <div key={group.label}>
                  <div className="px-2.5 pb-1 pt-2.5 text-[9px] font-semibold uppercase tracking-[0.15em] text-subtle/85">
                    {group.label}
                  </div>
                  {group.items.map((command) => {
                    flatIndex += 1;
                    const index = flatIndex;
                    const Icon = command.icon;
                    return (
                      <button
                        key={command.id}
                        type="button"
                        data-palette-index={index}
                        onMouseMove={() => setActiveIndex(index)}
                        onClick={() => runAt(index)}
                        className={cn(
                          "flex h-[38px] w-full items-center gap-2.5 rounded-lg px-2.5 text-left transition-colors",
                          activeIndex === index
                            ? "bg-[var(--bg-active)] text-fg"
                            : "text-muted hover:bg-[var(--bg-hover)] hover:text-fg",
                        )}
                      >
                        <span className="flex h-5 w-5 flex-none items-center justify-center">
                          {command.swatch ? (
                            <span
                              className="h-2.5 w-2.5 rounded-full ring-2 ring-inset ring-white/15"
                              style={{ background: command.swatch }}
                            />
                          ) : Icon ? (
                            <Icon
                              size={14}
                              className={
                                activeIndex === index
                                  ? "text-accent-text"
                                  : "text-subtle"
                              }
                            />
                          ) : null}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-[12.5px]">
                          {command.label}
                        </span>
                        {command.hint && (
                          <span className="truncate text-[10.5px] text-subtle">
                            {command.hint}
                          </span>
                        )}
                        {command.checked && (
                          <Check size={13} className="flex-none text-accent" />
                        )}
                        {command.shortcut && (
                          <span className="flex flex-none items-center gap-0.5">
                            <Kbd>{command.shortcut.split(" ")[0]}</Kbd>
                            <Kbd>{command.shortcut.split(" ")[1]}</Kbd>
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>

            <div className="flex h-9 items-center justify-between border-t border-line bg-surface/60 px-3.5 text-[10px] text-subtle">
              <span className="flex items-center gap-1.5">
                <Kbd>↑</Kbd>
                <Kbd>↓</Kbd>
                navigate
              </span>
              <span className="flex items-center gap-3">
                <span className="flex items-center gap-1.5">
                  <Kbd>↵</Kbd> select
                </span>
                <span className="flex items-center gap-1.5">
                  <Kbd>esc</Kbd> close
                </span>
              </span>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
