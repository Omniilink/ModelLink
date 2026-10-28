import { motion } from "framer-motion";
import {
  Blocks,
  BookText,
  Code2,
  MessageSquare,
  Settings2,
  type LucideIcon,
} from "lucide-react";

import { useApp } from "@/store/app";
import type { Section } from "@/types";

interface NavItem {
  id: Section;
  label: string;
  icon: LucideIcon;
  shortcut: string;
}

const primaryNav: NavItem[] = [
  { id: "chat", label: "Chat", icon: MessageSquare, shortcut: "Ctrl 1" },
  { id: "coding", label: "Coding", icon: Code2, shortcut: "Ctrl 2" },
  { id: "mcp", label: "MCP", icon: Blocks, shortcut: "Ctrl 3" },
  { id: "prompts", label: "Prompts", icon: BookText, shortcut: "Ctrl 4" },
];

function RailButton({ item }: { item: NavItem }) {
  const section = useApp((state) => state.section);
  const setSection = useApp((state) => state.setSection);
  const active = section === item.id;
  const Icon = item.icon;

  return (
    <button
      type="button"
      aria-label={item.label}
      aria-current={active ? "page" : undefined}
      data-active={active ? "true" : undefined}
      onClick={() => setSection(item.id)}
      className="rail-button rail-item group"
    >
      {active && (
        <motion.span
          layoutId="rail-active"
          className="absolute inset-0 rounded-[10px] border border-line bg-[var(--bg-active)]"
          transition={{ type: "spring", stiffness: 520, damping: 42 }}
        />
      )}
      <Icon
        size={18}
        strokeWidth={active ? 1.9 : 1.65}
        className="relative z-10"
      />
      <span className="rail-tooltip">
        {item.label}
        <span className="ml-2 font-mono text-[9.5px] text-subtle">
          {item.shortcut}
        </span>
      </span>
    </button>
  );
}

export function Rail() {
  const openSettings = useApp((state) => state.openSettings);
  const settingsSection = useApp((state) => state.settingsSection);
  const section = useApp((state) => state.section);
  const conversations = useApp((state) => state.conversations);

  return (
    <nav
      aria-label="Primary navigation"
      className="relative z-30 flex w-[58px] flex-none flex-col items-center border-r border-line bg-[color-mix(in_srgb,var(--bg-base)_38%,transparent)] py-2.5"
    >
      <div className="flex flex-col items-center gap-1">
        {primaryNav.map((item) => (
          <RailButton key={item.id} item={item} />
        ))}
      </div>

      <div className="flex-1" />

      <div className="mb-2 h-px w-6 bg-line" />

      <button
        type="button"
        aria-label="Settings"
        aria-current={section === "settings" ? "page" : undefined}
        data-active={section === "settings" ? "true" : undefined}
        onClick={() => openSettings(settingsSection)}
        className="rail-button rail-item group"
      >
        {section === "settings" && (
          <motion.span
            layoutId="rail-active"
            className="absolute inset-0 rounded-[10px] border border-line bg-[var(--bg-active)]"
            transition={{ type: "spring", stiffness: 520, damping: 42 }}
          />
        )}
        <Settings2
          size={18}
          strokeWidth={section === "settings" ? 1.9 : 1.65}
          className="relative z-10"
        />
        <span className="rail-tooltip">
          Settings
          <span className="ml-2 font-mono text-[9.5px] text-subtle">Ctrl ,</span>
        </span>
      </button>

      <button
        type="button"
        aria-label="Local profile"
        onClick={() => openSettings("account")}
        className="rail-button group relative mt-2 flex h-8 w-8 items-center justify-center rounded-full bg-[var(--bg-elevated)] text-[11px] font-semibold text-fg ring-1 ring-inset ring-line transition-colors hover:bg-[var(--bg-active)]"
      >
        <span className="relative z-10">G</span>
        <span className="absolute bottom-0 right-0 z-10 h-2 w-2 rounded-full border-2 border-base bg-success" />
        <span className="rail-tooltip">
          Guest
          <span className="ml-2 text-subtle">
            {conversations.length > 0
              ? `${conversations.length} chats on this device`
              : "Local only"}
          </span>
        </span>
      </button>
    </nav>
  );
}
