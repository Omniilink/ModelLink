import { Blocks, CloudOff, Cpu, HardDrive } from "lucide-react";

import { useApp } from "@/store/app";

export function StatusBar() {
  const openSettings = useApp((state) => state.openSettings);
  const setSection = useApp((state) => state.setSection);
  const mcpCount = useApp((state) => state.mcpServers.length);
  const conversations = useApp((state) => state.conversations.length);

  return (
    <footer className="chrome flex h-[25px] flex-none items-center gap-3 border-t border-line px-3 text-[10px] text-subtle">
      <span className="flex items-center gap-1.5">
        <span className="h-1.5 w-1.5 rounded-full bg-success" />
        <span className="font-medium text-muted">Local workspace</span>
      </span>
      <span className="h-3 w-px bg-line" />
      <span className="flex items-center gap-1.5">
        <HardDrive size={10} />
        {conversations} {conversations === 1 ? "chat" : "chats"}
      </span>
      <button
        type="button"
        onClick={() => setSection("mcp")}
        className="flex items-center gap-1.5 rounded px-1 py-px transition-colors hover:bg-hover hover:text-fg"
      >
        <Blocks size={10} />
        {mcpCount} MCP
      </button>

      <div className="flex-1" />

      <button
        type="button"
        onClick={() => openSettings("account")}
        className="flex items-center gap-1.5 rounded px-1 py-px transition-colors hover:bg-hover hover:text-fg"
      >
        <CloudOff size={10} />
        Sync off
      </button>
      <span className="h-3 w-px bg-line" />
      <button
        type="button"
        onClick={() => openSettings("models")}
        className="flex items-center gap-1.5 rounded px-1 py-px transition-colors hover:bg-hover hover:text-fg"
      >
        <Cpu size={10} />
        No model connected
      </button>
      <span className="h-3 w-px bg-line" />
      <span className="font-mono tracking-tight">v0.1.0</span>
    </footer>
  );
}
