import { AnimatePresence, motion } from "framer-motion";
import {
  BarChart3,
  BookText,
  Code2,
  Database,
  DownloadCloud,
  FolderKanban,
  HardDrive,
  Info,
  Palette,
  Plus,
  Search,
  ShieldCheck,
  Terminal,
  Trash2,
  UserRound,
  Waypoints,
  Cpu,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { IconButton } from "@/components/ui/IconButton";
import { useApp } from "@/store/app";
import type { Conversation, SettingsSection } from "@/types";
import { cn, formatDayLabel, formatRelative, formatTime } from "@/lib/utils";

const settingsItems: {
  id: SettingsSection;
  label: string;
  icon: typeof Palette;
}[] = [
  { id: "appearance", label: "Appearance", icon: Palette },
  { id: "models", label: "Models", icon: Cpu },
  { id: "editor", label: "Editor", icon: Code2 },
  { id: "data", label: "Data & storage", icon: Database },
  { id: "account", label: "Account", icon: UserRound },
  { id: "analytics", label: "Analytics", icon: BarChart3 },
  { id: "privacy", label: "Privacy", icon: ShieldCheck },
  { id: "updates", label: "Updates", icon: DownloadCloud },
  { id: "about", label: "About", icon: Info },
];

function PaneHeader({
  title,
  count,
  onAdd,
  addLabel,
}: {
  title: string;
  count?: number;
  onAdd?: () => void;
  addLabel?: string;
}) {
  return (
    <div className="flex h-11 flex-none items-center justify-between pl-4 pr-2.5">
      <div className="flex items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-subtle">
          {title}
        </span>
        {count !== undefined && (
          <span className="rounded-full bg-[var(--bg-hover)] px-1.5 py-px text-[9px] font-medium tabular-nums text-subtle">
            {count}
          </span>
        )}
      </div>
      {onAdd && (
        <IconButton label={addLabel ?? "Add"} onClick={onAdd} className="h-6 w-6">
          <Plus size={14} />
        </IconButton>
      )}
    </div>
  );
}

function SearchField({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <div className="relative px-3 pb-2">
      <Search
        size={12}
        className="pointer-events-none absolute left-[19px] top-1/2 -translate-y-1/2 text-subtle"
      />
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="h-7 w-full rounded-md border border-line bg-[color-mix(in_srgb,var(--bg-base)_62%,transparent)] pl-7 pr-2 text-[11.5px] text-fg outline-none transition-colors placeholder:text-subtle hover:border-line-strong focus:border-[var(--accent-border)] focus:bg-base"
      />
    </div>
  );
}

function PaneFooter({
  icon: Icon,
  children,
}: {
  icon: typeof ShieldCheck;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-none items-start gap-2 border-t border-line px-4 py-3 text-[10.5px] leading-relaxed text-subtle">
      <Icon size={12} className="mt-0.5 flex-none opacity-70" />
      <span>{children}</span>
    </div>
  );
}

function stripMarkdown(value: string) {
  return value
    .replace(/```[\s\S]*?```/g, " code ")
    .replace(/[#>*_`~\-]/g, " ")
    .replace(/\[(.*?)\]\(.*?\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function ChatPane() {
  const conversations = useApp((state) => state.conversations);
  const activeId = useApp((state) => state.activeConversationId);
  const setActiveConversation = useApp(
    (state) => state.setActiveConversation,
  );
  const newConversation = useApp((state) => state.newConversation);
  const deleteConversation = useApp((state) => state.deleteConversation);
  const pushToast = useApp((state) => state.pushToast);
  const [query, setQuery] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);

  const groups = useMemo(() => {
    const term = query.trim().toLowerCase();
    const filtered = conversations
      .filter((conversation) => {
        if (!term) return true;
        const last = conversation.messages.at(-1)?.content ?? "";
        return (
          conversation.title.toLowerCase().includes(term) ||
          last.toLowerCase().includes(term)
        );
      })
      .sort((a, b) => b.updatedAt - a.updatedAt);

    const grouped: { label: string; items: Conversation[] }[] = [];
    for (const conversation of filtered) {
      const label = formatDayLabel(conversation.updatedAt);
      const existing = grouped.at(-1);
      if (existing && existing.label === label) {
        existing.items.push(conversation);
      } else {
        grouped.push({ label, items: [conversation] });
      }
    }
    return grouped;
  }, [conversations, query]);

  return (
    <>
      <PaneHeader
        title="Chats"
        count={conversations.length}
        onAdd={() => newConversation()}
        addLabel="New chat"
      />
      <SearchField
        value={query}
        onChange={setQuery}
        placeholder="Search conversations"
      />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto pb-2">
        {groups.length === 0 && (
          <div className="px-5 py-10 text-center">
            <p className="text-[11.5px] text-muted">No conversations found.</p>
            <button
              type="button"
              className="btn btn-ghost mt-2 text-[11px]"
              onClick={() => {
                setQuery("");
                newConversation();
              }}
            >
              Start a new chat
            </button>
          </div>
        )}
        {groups.map((group) => (
          <div key={`${group.label}-${group.items[0]?.id}`}>
            <div className="px-4 pb-1 pt-3 text-[9.5px] font-semibold uppercase tracking-[0.14em] text-subtle/85">
              {group.label}
            </div>
            {group.items.map((conversation) => {
              const active = conversation.id === activeId;
              const last = conversation.messages.at(-1);
              const preview = last
                ? stripMarkdown(last.content).slice(0, 76)
                : "No messages yet";
              const time =
                formatDayLabel(conversation.updatedAt) === "Today"
                  ? formatTime(conversation.updatedAt)
                  : formatRelative(conversation.updatedAt);
              return (
                <div key={conversation.id} className="group relative px-2">
                  <button
                    type="button"
                    onClick={() => setActiveConversation(conversation.id)}
                    className={cn(
                      "relative flex w-full flex-col gap-0.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[var(--bg-hover)]",
                      active && "bg-[var(--bg-active)]",
                    )}
                  >
                    {active && (
                      <span className="absolute left-0 top-1/2 h-3.5 w-[2px] -translate-y-1/2 rounded-full bg-accent" />
                    )}
                    <span className="flex min-w-0 items-center gap-2">
                      <span
                        className={cn(
                          "truncate pr-10 text-[12px] font-medium",
                          active ? "text-fg" : "text-[color-mix(in_srgb,var(--fg)_86%,var(--fg-subtle))]",
                        )}
                      >
                        {conversation.title}
                      </span>
                      <span className="absolute right-2.5 top-2 text-[9.5px] tabular-nums text-subtle transition-opacity group-hover:opacity-0">
                        {time}
                      </span>
                    </span>
                    <span className="truncate pr-2 text-[10.5px] leading-relaxed text-subtle">
                      {preview || "No messages yet"}
                    </span>
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete ${conversation.title}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      setDeleteTarget(conversation);
                    }}
                    className="absolute right-2 top-1.5 flex h-6 w-6 items-center justify-center rounded text-subtle opacity-0 transition-all hover:bg-[color-mix(in_srgb,var(--danger)_12%,transparent)] hover:text-danger focus:opacity-100 group-hover:opacity-100"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              );
            })}
          </div>
        ))}
      </div>
      <PaneFooter icon={ShieldCheck}>
        Conversations are stored on this device. Sign-in and sync arrive with
        the cloud account layer.
      </PaneFooter>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Delete conversation?"
        description={
          deleteTarget
            ? `"${deleteTarget.title}" and its messages will be removed from this device.`
            : ""
        }
        confirmLabel="Delete chat"
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => {
          if (!deleteTarget) return;
          deleteConversation(deleteTarget.id);
          pushToast({
            title: "Conversation deleted",
            description: "The chat has been removed from this device.",
          });
          setDeleteTarget(null);
        }}
      />
    </>
  );
}

function CodingPane() {
  const projects = useApp((state) => state.projects);
  const activeId = useApp((state) => state.activeProjectId);
  const workspaces = useApp((state) => state.workspaces);
  const activeWorkspaceId = useApp((state) => state.activeWorkspaceId);
  const setActiveProject = useApp((state) => state.setActiveProject);
  const setActiveWorkspace = useApp((state) => state.setActiveWorkspace);
  const addWorkspace = useApp((state) => state.addWorkspace);
  const openModal = useApp((state) => state.openModal);
  const pushToast = useApp((state) => state.pushToast);
  const [query, setQuery] = useState("");

  const filtered = projects
    .filter((project) => (project.workspaceId ?? "default") === (activeWorkspaceId ?? "default"))
    .filter((project) =>
      project.name.toLowerCase().includes(query.trim().toLowerCase()),
    )
    .sort((a, b) => b.updatedAt - a.updatedAt);

  return (
    <>
      <div className="flex h-11 flex-none items-center justify-between pl-4 pr-2.5">
        <select
          value={activeWorkspaceId ?? "default"}
          onChange={(event) => setActiveWorkspace(event.target.value)}
          className="h-6 rounded-md border border-line bg-[var(--bg-base)] px-2 text-[10.5px] text-fg outline-none"
        >
          {workspaces.map((workspace) => (
            <option key={workspace.id} value={workspace.id}>
              {workspace.name}
            </option>
          ))}
        </select>
        <IconButton
          label="New workspace"
          onClick={() => {
            const name = window.prompt("Workspace name");
            if (name) {
              addWorkspace(name);
              pushToast({ title: "Workspace created", tone: "success" });
            }
          }}
          className="h-6 w-6"
        >
          <Plus size={12} />
        </IconButton>
      </div>
      <PaneHeader
        title="Projects"
        count={filtered.length}
        onAdd={() => openModal({ type: "project-new" })}
        addLabel="New project"
      />
      <SearchField
        value={query}
        onChange={setQuery}
        placeholder="Search projects"
      />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {filtered.length === 0 && (
          <p className="px-3 py-8 text-center text-[11.5px] text-muted">
            No projects yet.
          </p>
        )}
        {filtered.map((project) => {
          const active = project.id === activeId;
          return (
            <button
              key={project.id}
              type="button"
              onClick={() => setActiveProject(project.id)}
              className={cn(
                "relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[var(--bg-hover)]",
                active && "bg-[var(--bg-active)]",
              )}
            >
              {active && (
                <span className="absolute left-0 top-1/2 h-3.5 w-[2px] -translate-y-1/2 rounded-full bg-accent" />
              )}
              <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-muted">
                <Waypoints size={13} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1.5">
                  <span className="truncate text-[12px] font-medium text-fg">
                    {project.name}
                  </span>
                  {project.sample && (
                    <span className="flex-none rounded border border-line px-1 py-px text-[8.5px] uppercase tracking-wide text-subtle">
                      sample
                    </span>
                  )}
                </span>
                <span className="mt-0.5 block truncate text-[10px] text-subtle">
                  {project.files.length} files · {formatRelative(project.updatedAt)}
                </span>
              </span>
            </button>
          );
        })}
      </div>
      <PaneFooter icon={HardDrive}>
        Projects are saved with your local workspace. Per-file sync plugs in
        once an account is connected.
      </PaneFooter>
    </>
  );
}

function McpPane() {
  const servers = useApp((state) => state.mcpServers);
  const openModal = useApp((state) => state.openModal);
  const [query, setQuery] = useState("");

  const filtered = servers.filter((server) =>
    server.name.toLowerCase().includes(query.trim().toLowerCase()),
  );

  return (
    <>
      <PaneHeader
        title="Servers"
        count={servers.length}
        onAdd={() => openModal({ type: "mcp-new" })}
        addLabel="Add server"
      />
      <SearchField
        value={query}
        onChange={setQuery}
        placeholder="Search servers"
      />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {filtered.length === 0 && (
          <div className="px-3 py-8 text-center">
            <p className="text-[11.5px] leading-relaxed text-muted">
              No MCP servers configured.
            </p>
            <button
              type="button"
              onClick={() => openModal({ type: "mcp-new" })}
              className="btn btn-ghost mt-2 text-[11px]"
            >
              Add your first server
            </button>
          </div>
        )}
        {filtered.map((server) => (
          <button
            key={server.id}
            type="button"
            onClick={() => openModal({ type: "mcp-edit", id: server.id })}
            className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[var(--bg-hover)]"
          >
            <span className="relative flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-muted">
              <Terminal size={13} />
              <span
                className={cn(
                  "absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ring-2 ring-[var(--pane)]",
                  server.enabled ? "bg-success" : "bg-[var(--fg-subtle)]",
                )}
              />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[12px] font-medium text-fg">
                {server.name}
              </span>
              <span className="mt-0.5 block truncate font-mono text-[9.5px] text-subtle">
                {server.transport === "stdio"
                  ? server.command || "stdio"
                  : server.url || server.transport.toUpperCase()}
              </span>
            </span>
            <span className="flex-none rounded border border-line px-1 py-px text-[8.5px] font-medium uppercase tracking-wide text-subtle">
              {server.transport}
            </span>
          </button>
        ))}
      </div>
      <PaneFooter icon={Terminal}>
        MCP configuration is local. Process supervision and tool discovery
        arrive with the agent runtime.
      </PaneFooter>
    </>
  );
}

function PromptsPane() {
  const prompts = useApp((state) => state.prompts);
  const promptTag = useApp((state) => state.promptTag);
  const setPromptTag = useApp((state) => state.setPromptTag);
  const openModal = useApp((state) => state.openModal);

  const tags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const prompt of prompts) {
      for (const tag of prompt.tags) {
        counts.set(tag, (counts.get(tag) ?? 0) + 1);
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [prompts]);

  return (
    <>
      <PaneHeader
        title="Library"
        count={prompts.length}
        onAdd={() => openModal({ type: "prompt-new" })}
        addLabel="New prompt"
      />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-2 pt-1">
        <div className="px-2 pb-1 pt-1 text-[9.5px] font-semibold uppercase tracking-[0.14em] text-subtle/85">
          Collections
        </div>
        <button
          type="button"
          onClick={() => setPromptTag(null)}
          className={cn(
            "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[11.5px] transition-colors hover:bg-[var(--bg-hover)]",
            promptTag === null ? "bg-[var(--bg-active)] text-fg" : "text-muted",
          )}
        >
          <BookText size={13} className="flex-none opacity-70" />
          <span className="flex-1">All prompts</span>
          <span className="text-[9.5px] tabular-nums text-subtle">
            {prompts.length}
          </span>
        </button>
        {tags.map(([tag, count]) => (
          <button
            key={tag}
            type="button"
            onClick={() => setPromptTag(tag === promptTag ? null : tag)}
            className={cn(
              "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[11.5px] transition-colors hover:bg-[var(--bg-hover)]",
              promptTag === tag ? "bg-[var(--bg-active)] text-fg" : "text-muted",
            )}
          >
            <span
              className="h-1.5 w-1.5 flex-none rounded-full"
              style={{
                background:
                  promptTag === tag ? "var(--accent)" : "var(--fg-subtle)",
              }}
            />
            <span className="flex-1 truncate">{tag}</span>
            <span className="text-[9.5px] tabular-nums text-subtle">
              {count}
            </span>
          </button>
        ))}

        <div className="mt-4 px-2 pb-1 pt-1 text-[9.5px] font-semibold uppercase tracking-[0.14em] text-subtle/85">
          Recent
        </div>
        {prompts
          .slice()
          .sort((a, b) => b.updatedAt - a.updatedAt)
          .slice(0, 6)
          .map((prompt) => (
            <button
              key={prompt.id}
              type="button"
              onClick={() => openModal({ type: "prompt-edit", id: prompt.id })}
              className="block w-full truncate rounded-lg px-2.5 py-1.5 text-left text-[11px] text-muted transition-colors hover:bg-[var(--bg-hover)] hover:text-fg"
            >
              {prompt.title}
            </button>
          ))}
      </div>
      <PaneFooter icon={BookText}>
        Reuse an instruction in any chat with one click. Edit a prompt to keep
        it sharp.
      </PaneFooter>
    </>
  );
}

function SettingsPane() {
  const settingsSection = useApp((state) => state.settingsSection);
  const setSettingsSection = useApp((state) => state.setSettingsSection);

  return (
    <>
      <PaneHeader title="Settings" />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pt-1">
        {settingsItems.map((item) => {
          const Icon = item.icon;
          const active = settingsSection === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setSettingsSection(item.id)}
              className={cn(
                "relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[var(--bg-hover)]",
                active && "bg-[var(--bg-active)]",
              )}
            >
              {active && (
                <span className="absolute left-0 top-1/2 h-3.5 w-[2px] -translate-y-1/2 rounded-full bg-accent" />
              )}
              <Icon
                size={14}
                className={active ? "text-accent-text" : "text-subtle"}
              />
              <span
                className={cn(
                  "text-[12px] font-medium",
                  active ? "text-fg" : "text-muted",
                )}
              >
                {item.label}
              </span>
            </button>
          );
        })}
      </div>
      <div className="flex-none border-t border-line px-4 py-3 text-[10px] leading-relaxed text-subtle">
        <div className="flex items-center justify-between">
          <span>OmniLink Desktop</span>
          <span className="font-mono">v0.1.0</span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-success" />
          Local workspace
        </div>
      </div>
    </>
  );
}

function AssetsPane() {
  const assets = useApp((state) => state.assets);
  return (
    <>
      <PaneHeader title="Assets" count={assets.length} />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4 py-3 text-[11px] text-subtle">
        {assets.length === 0
          ? "No assets yet. Add images or 3D models from the Assets view."
          : `${assets.length} asset${assets.length === 1 ? "" : "s"} in this workspace.`}
      </div>
    </>
  );
}

function WorkspacesPane() {
  const workspaces = useApp((state) => state.workspaces);
  const activeWorkspaceId = useApp((state) => state.activeWorkspaceId);
  const setActiveWorkspace = useApp((state) => state.setActiveWorkspace);
  return (
    <>
      <PaneHeader title="Workspaces" count={workspaces.length} />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-2 pt-1">
        {workspaces.map((workspace) => {
          const active = workspace.id === activeWorkspaceId;
          return (
            <button
              key={workspace.id}
              type="button"
              onClick={() => setActiveWorkspace(workspace.id)}
              className={cn(
                "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[var(--bg-hover)]",
                active && "bg-[var(--bg-active)]",
              )}
            >
              <FolderKanban size={13} className="flex-none text-muted" />
              <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-fg">
                {workspace.name}
              </span>
            </button>
          );
        })}
      </div>
    </>
  );
}
export function ContextPane() {
  const section = useApp((state) => state.section);

  return (
    <div className="flex h-full w-[268px] flex-col">
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={section}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          className="flex min-h-0 flex-1 flex-col"
        >
          {section === "chat" && <ChatPane />}
          {section === "coding" && <CodingPane />}
          {section === "workspaces" && <WorkspacesPane />}
          {section === "mcp" && <McpPane />}
          {section === "prompts" && <PromptsPane />}
          {section === "assets" && <AssetsPane />}
          {section === "settings" && <SettingsPane />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}
