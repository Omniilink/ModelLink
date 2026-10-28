import {
  ChevronRight,
  CloudOff,
  PanelLeft,
  Search,
} from "lucide-react";

import { Kbd, modKey } from "@/components/ui/Kbd";
import { Logo } from "@/components/ui/Logo";
import { WindowControls } from "@/components/shell/WindowControls";
import { windowControls } from "@/lib/tauri";
import { useApp } from "@/store/app";
import type { Section } from "@/types";

const sectionNames: Record<Section, string> = {
  chat: "Chat",
  coding: "Coding",
  mcp: "MCP",
  prompts: "Prompts",
  settings: "Settings",
};

export function TitleBar() {
  const section = useApp((state) => state.section);
  const setPaletteOpen = useApp((state) => state.setPaletteOpen);
  const toggleContext = useApp((state) => state.toggleContext);
  const contextCollapsed = useApp((state) => state.contextCollapsed);
  const openSettings = useApp((state) => state.openSettings);

  return (
    <header className="titlebar chrome">
      <div
        className="titlebar-drag"
        data-tauri-drag-region
        onDoubleClick={() => {
          windowControls()?.toggleMaximize().catch(() => undefined);
        }}
      />

      <div className="pointer-events-none relative z-10 flex h-full w-full items-center px-3">
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <Logo size={20} />
          <span className="text-[12.5px] font-semibold tracking-[-0.015em] text-fg">
            OmniLink
          </span>
          <span className="rounded-full border border-line bg-[var(--bg-hover)] px-1.5 py-px text-[8.5px] font-semibold uppercase tracking-[0.12em] text-subtle">
            alpha
          </span>
          <div className="mx-1 h-3.5 w-px bg-line" />
          <div className="hidden min-w-0 items-center gap-1.5 text-[11px] text-subtle min-[1180px]:flex">
            <span>Workspace</span>
            <ChevronRight size={11} className="opacity-60" />
            <span className="truncate font-medium text-muted">
              {sectionNames[section]}
            </span>
          </div>
        </div>

        <button
          type="button"
          onClick={() => setPaletteOpen(true)}
          className="pointer-events-auto group flex h-[27px] w-[min(360px,34vw)] flex-none items-center gap-2 rounded-lg border border-line bg-[color-mix(in_srgb,var(--bg-base)_55%,transparent)] px-2.5 text-left transition-colors hover:border-line-strong hover:bg-[var(--bg-hover)]"
        >
          <Search size={13} className="flex-none text-subtle" />
          <span className="flex-1 truncate text-[11.5px] text-subtle group-hover:text-muted">
            Search or jump to…
          </span>
          <span className="flex items-center gap-0.5">
            <Kbd>{modKey}</Kbd>
            <Kbd>K</Kbd>
          </span>
        </button>

        <div className="flex min-w-0 flex-1 items-center justify-end gap-2">
          <button
            type="button"
            onClick={() => openSettings("account")}
            title="Local workspace — no account connected"
            className="pointer-events-auto hidden items-center gap-1.5 rounded-full border border-line bg-[var(--bg-hover)] px-2 py-1 text-[10px] font-medium text-muted transition-colors hover:border-line-strong hover:text-fg min-[1120px]:flex"
          >
            <CloudOff size={11} />
            Local
          </button>
          <button
            type="button"
            onClick={toggleContext}
            title={contextCollapsed ? "Show sidebar" : "Hide sidebar"}
            aria-label={contextCollapsed ? "Show sidebar" : "Hide sidebar"}
            aria-pressed={!contextCollapsed}
            data-active={!contextCollapsed ? "true" : undefined}
            className="icon-button pointer-events-auto h-7 w-7"
          >
            <PanelLeft size={14} />
          </button>
          <div className="mx-1 h-4 w-px bg-line" />
          <div className="pointer-events-auto h-full">
            <WindowControls />
          </div>
        </div>
      </div>
    </header>
  );
}
