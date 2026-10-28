import {
  Blocks,
  Check,
  Copy,
  Globe,
  Pencil,
  Plus,
  Plug,
  Radio,
  Terminal,
  Trash2,
} from "lucide-react";
import { useState } from "react";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { IconButton } from "@/components/ui/IconButton";
import { Switch } from "@/components/ui/Switch";
import { copyText } from "@/lib/utils";
import { mcpProbeHttp, mcpProbeStdio, mcpCallToolStdio, mcpCallToolHttp, mcpListResourcesStdio, mcpListResourcesHttp, runShellCommand, httpRequest } from "@/lib/tauri";
import { useApp } from "@/store/app";
import type { McpServer, McpTransport } from "@/types";

const transportMeta: Record<
  McpTransport,
  { label: string; description: string; icon: typeof Terminal }
> = {
  stdio: {
    label: "stdio",
    description: "Local process",
    icon: Terminal,
  },
  sse: {
    label: "SSE",
    description: "Server-sent events",
    icon: Radio,
  },
  http: {
    label: "HTTP",
    description: "Streamable HTTP",
    icon: Globe,
  },
};

const sampleConfig = `{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "C:\\\\projects"
      ]
    }
  }
}`;

function TransportIcon({
  transport,
  size = 13,
}: {
  transport: McpTransport;
  size?: number;
}) {
  const Icon = transportMeta[transport].icon;
  return <Icon size={size} />;
}

function ServerRow({
  server,
  onDelete,
  onBrowseTools,
}: {
  server: McpServer;
  onDelete: (server: McpServer) => void;
  onBrowseTools: (server: McpServer) => void;
}) {
  const openModal = useApp((state) => state.openModal);
  const toggleMcpServer = useApp((state) => state.toggleMcpServer);
  const pushToast = useApp((state) => state.pushToast);
  const [probing, setProbing] = useState(false);

  const testConnection = async () => {
    setProbing(true);
    try {
      const tools =
        server.transport === "stdio"
          ? await mcpProbeStdio(server.command, server.args)
          : await mcpProbeHttp(server.url);
      pushToast({
        title: `${server.name}: ${tools.length} tool${
          tools.length === 1 ? "" : "s"
        } available`,
        description: tools.map((tool) => tool.name).slice(0, 6).join(", "),
        tone: "success",
      });
    } catch (error) {
      pushToast({
        title: `${server.name}: connection failed`,
        description: error instanceof Error ? error.message : String(error),
        tone: "danger",
      });
    } finally {
      setProbing(false);
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => openModal({ type: "mcp-edit", id: server.id })}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openModal({ type: "mcp-edit", id: server.id });
        }
      }}
      className="group flex cursor-pointer items-center gap-3 border-b border-line px-4 py-3 transition-colors last:border-b-0 hover:bg-[var(--bg-hover)]"
    >
      <span className="relative flex h-8 w-8 flex-none items-center justify-center rounded-lg border border-line bg-[var(--bg-elevated)] text-muted">
        <TransportIcon transport={server.transport} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[12.5px] font-medium text-fg">
            {server.name}
          </span>
          <span className="rounded border border-line px-1 py-px font-mono text-[8px] uppercase tracking-wide text-subtle">
            {transportMeta[server.transport].label}
          </span>
        </div>
        <p className="mt-0.5 truncate font-mono text-[10px] text-subtle">
          {server.transport === "stdio"
            ? `${server.command} ${server.args}`.trim() || "No command"
            : server.url || "No endpoint"}
        </p>
      </div>
      <span className="hidden w-[88px] flex-none items-center gap-1.5 text-[10px] text-subtle lg:flex">
        <span
          className={
            server.enabled
              ? "h-1.5 w-1.5 rounded-full bg-success"
              : "h-1.5 w-1.5 rounded-full bg-[var(--fg-subtle)]"
          }
        />
        {server.enabled ? "Enabled" : "Disabled"}
      </span>
      <div
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        <Switch
          checked={server.enabled}
          onChange={() => toggleMcpServer(server.id)}
          label={`${server.enabled ? "Disable" : "Enable"} ${server.name}`}
        />
      </div>
      <div
        className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        <IconButton
          label="Test connection"
          onClick={() => void testConnection()}
          className="h-6 w-6"
        >
          <Plug size={12} className={probing ? "animate-pulse" : ""} />
        </IconButton>
        <IconButton
          label="Browse tools"
          onClick={() => onBrowseTools(server)}
          className="h-6 w-6"
        >
          <Terminal size={12} />
        </IconButton>
        <IconButton
          label="Edit server"
          onClick={() => openModal({ type: "mcp-edit", id: server.id })}
          className="h-6 w-6"
        >
          <Pencil size={12} />
        </IconButton>
        <IconButton
          label="Delete server"
          onClick={() => onDelete(server)}
          className="h-6 w-6 hover:text-danger"
        >
          <Trash2 size={12} />
        </IconButton>
      </div>
    </div>
  );
}

export function McpView() {
  const servers = useApp((state) => state.mcpServers);
  const openModal = useApp((state) => state.openModal);
  const pushToast = useApp((state) => state.pushToast);
  const deleteMcpServer = useApp((state) => state.deleteMcpServer);
  const addMcpServer = useApp((state) => state.addMcpServer);
  const [toolsMap, setToolsMap] = useState<Record<string, { name: string; description?: string }[]>>({});
  const [resourcesMap, setResourcesMap] = useState<Record<string, { uri: string; name?: string; description?: string }[]>>({});
  const [selectedServer, setSelectedServer] = useState<McpServer | null>(null);
  const [toolArgs, setToolArgs] = useState("{}");
  const [toolResult, setToolResult] = useState("");
  const [invoking, setInvoking] = useState(false);
  const [customTools, setCustomTools] = useState<{ name: string; type: "shell" | "http"; command: string; url: string }[]>([]);
  const [customName, setCustomName] = useState("");
  const [customType, setCustomType] = useState<"shell" | "http">("shell");
  const [customCommand, setCustomCommand] = useState("");
  const [customUrl, setCustomUrl] = useState("");
  const [customResult, setCustomResult] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<McpServer | null>(null);
  const [copied, setCopied] = useState(false);

  const enabled = servers.filter((server) => server.enabled).length;
  const remote = servers.filter((server) => server.transport !== "stdio").length;

  const copySample = async () => {
    try {
      await copyText(sampleConfig);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
      pushToast({ title: "Example configuration copied", tone: "success" });
    } catch {
      pushToast({ title: "Could not access the clipboard", tone: "danger" });
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[52px] flex-none items-center gap-3 border-b border-line px-6">
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-muted">
          <Blocks size={14} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="text-[13px] font-semibold tracking-[-0.01em] text-fg">
              MCP servers
            </h1>
            {servers.length > 0 && (
              <span className="rounded-full bg-[var(--bg-hover)] px-1.5 py-px text-[9px] tabular-nums text-subtle">
                {servers.length}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[10px] text-subtle">
            Give agents access to local processes and remote tools
          </p>
        </div>
        {servers.length > 0 && (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => openModal({ type: "mcp-new" })}
          >
            <Plus size={13} />
            Add server
          </button>
        )}
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {servers.length === 0 ? (
          <div className="mx-auto w-full max-w-[760px] px-6 py-12">
            <div className="flex flex-col items-center text-center">
              <div className="relative">
                <div className="absolute inset-0 rounded-2xl bg-accent opacity-20 blur-2xl" />
                <div className="relative flex h-14 w-14 items-center justify-center rounded-2xl border border-line bg-[var(--bg-elevated)] text-accent-text shadow-xl">
                  <Blocks size={24} />
                </div>
              </div>
              <h2 className="mt-5 text-[22px] font-semibold tracking-[-0.025em] text-fg">
                Give your agents tools
              </h2>
              <p className="mt-2 max-w-[430px] text-[12.5px] leading-relaxed text-muted">
                Register Model Context Protocol servers once, then reuse them
                across chat and coding sessions. OmniLink supports local
                processes and remote endpoints.
              </p>
              <button
                type="button"
                className="btn btn-primary mt-5"
                onClick={() => openModal({ type: "mcp-new" })}
              >
                <Plus size={13} />
                Add your first server
              </button>
            </div>

            <div className="mt-6">
              <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
                Popular servers
              </p>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {[
                  { name: "filesystem", command: "npx", args: "-y @modelcontextprotocol/server-filesystem ." },
                  { name: "fetch", command: "uvx", args: "mcp-server-fetch" },
                ].map((s) => (
                  <div
                    key={s.name}
                    className="flex items-center justify-between rounded-lg border border-line bg-[var(--bg-surface)] px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="text-[11.5px] font-medium text-fg">{s.name}</p>
                      <p className="truncate font-mono text-[9px] text-subtle">
                        {s.command} {s.args}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        addMcpServer({
                          name: s.name,
                          transport: "stdio",
                          command: s.command,
                          args: s.args,
                          url: "",
                        });
                        pushToast({ title: `${s.name} added`, tone: "success" });
                      }}
                      className="btn h-7 text-[10px]"
                    >
                      Add
                    </button>
                  </div>
                ))}
              </div>
            </div>

            <div className="mt-9 grid grid-cols-1 gap-2 sm:grid-cols-3">
              {(Object.keys(transportMeta) as McpTransport[]).map((key) => {
                const meta = transportMeta[key];
                const Icon = meta.icon;
                return (
                  <div
                    key={key}
                    className="rounded-xl border border-line bg-[var(--bg-surface)] p-3.5"
                  >
                    <span className="flex h-7 w-7 items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-muted">
                      <Icon size={13} />
                    </span>
                    <p className="mt-2.5 flex items-center gap-2 text-[11.5px] font-medium text-fg">
                      {meta.label}
                      <span className="rounded border border-line px-1 py-px text-[8px] uppercase tracking-wide text-subtle">
                        supported
                      </span>
                    </p>
                    <p className="mt-0.5 text-[10px] leading-relaxed text-subtle">
                      {meta.description}
                    </p>
                  </div>
                );
              })}
            </div>

            <div className="mt-4 overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]">
              <div className="flex items-center justify-between border-b border-line px-3.5 py-2">
                <span className="text-[10px] font-medium text-muted">
                  Example configuration
                </span>
                <button
                  type="button"
                  onClick={() => void copySample()}
                  className="flex items-center gap-1.5 rounded px-1.5 py-1 text-[10px] text-subtle transition-colors hover:bg-hover hover:text-fg"
                >
                  {copied ? (
                    <Check size={11} className="text-success" />
                  ) : (
                    <Copy size={11} />
                  )}
                  {copied ? "Copied" : "Copy JSON"}
                </button>
              </div>
              <pre className="overflow-x-auto bg-[color-mix(in_srgb,var(--bg-base)_60%,transparent)] p-3.5 font-mono text-[10.5px] leading-relaxed text-muted">
                {sampleConfig}
              </pre>
            </div>
          </div>
        ) : (
          <div className="mx-auto w-full max-w-[900px] px-6 py-6">
            <div className="mb-5 grid grid-cols-3 gap-2">
              {[
                { label: "Configured", value: servers.length },
                { label: "Enabled", value: enabled },
                { label: "Remote", value: remote },
              ].map((stat) => (
                <div
                  key={stat.label}
                  className="rounded-xl border border-line bg-[var(--bg-surface)] px-4 py-3"
                >
                  <p className="text-[9.5px] font-medium uppercase tracking-[0.12em] text-subtle">
                    {stat.label}
                  </p>
                  <p className="mt-1 text-[20px] font-semibold tabular-nums tracking-tight text-fg">
                    {stat.value}
                  </p>
                </div>
              ))}
            </div>

            <div className="overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]">
              <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
                <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
                  Servers
                </span>
                <span className="text-[10px] text-subtle">
                  {enabled} of {servers.length} enabled
                </span>
              </div>
              {servers.map((server) => (
                <ServerRow
                  key={server.id}
                  server={server}
                  onDelete={setDeleteTarget}
                  onBrowseTools={(s) => {
                    setSelectedServer(s);
                    // fetch tools
                    const fetchTools = async () => {
                      try {
                        const tools =
                          s.transport === "stdio"
                            ? await mcpProbeStdio(s.command, s.args)
                            : await mcpProbeHttp(s.url);
                        setToolsMap((prev) => ({ ...prev, [s.id]: tools }));
                        // also fetch resources
                        try {
                          const res =
                            s.transport === "stdio"
                              ? await mcpListResourcesStdio(s.command, s.args)
                              : await mcpListResourcesHttp(s.url);
                          setResourcesMap((prev) => ({ ...prev, [s.id]: res }));
                        } catch {
                          // resources not supported
                        }
                      } catch (e) {
                        pushToast({ title: "Failed to list tools", tone: "danger" });
                      }
                    };
                    void fetchTools();
                  }}
                />
              ))}
            </div>

            <p className="mt-3 px-1 text-[10px] leading-relaxed text-subtle">
              Server processes are supervised by the agent runtime when a
              session starts. Configuration itself is stored locally.
            </p>

            <div className="mt-5 overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]">
              <div className="border-b border-line px-4 py-2.5">
                <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
                  Custom tools
                </span>
              </div>
              <div className="p-4">
                <div className="grid gap-2 sm:grid-cols-2">
                  <input
                    value={customName}
                    onChange={(e) => setCustomName(e.target.value)}
                    placeholder="Tool name"
                    className="field h-8 text-[11px]"
                  />
                  <select
                    value={customType}
                    onChange={(e) => setCustomType(e.target.value as "shell" | "http")}
                    className="field h-8 text-[11px]"
                  >
                    <option value="shell">Shell command</option>
                    <option value="http">HTTP request</option>
                  </select>
                  {customType === "shell" ? (
                    <input
                      value={customCommand}
                      onChange={(e) => setCustomCommand(e.target.value)}
                      placeholder="Command (e.g. echo hello)"
                      className="field h-8 text-[11px] sm:col-span-2"
                    />
                  ) : (
                    <input
                      value={customUrl}
                      onChange={(e) => setCustomUrl(e.target.value)}
                      placeholder="URL (https://...)"
                      className="field h-8 text-[11px] sm:col-span-2"
                    />
                  )}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={async () => {
                      if (!customName.trim()) {
                        pushToast({ title: "Name required", tone: "danger" });
                        return;
                      }
                      setCustomTools((prev) => [
                        ...prev,
                        {
                          name: customName.trim(),
                          type: customType,
                          command: customCommand,
                          url: customUrl,
                        },
                      ]);
                      setCustomName("");
                      setCustomCommand("");
                      setCustomUrl("");
                      pushToast({ title: "Custom tool added", tone: "success" });
                    }}
                    className="btn btn-primary h-7 text-[10.5px]"
                  >
                    Add tool
                  </button>
                </div>
                {customTools.length > 0 && (
                  <div className="mt-3 space-y-2">
                    {customTools.map((ct, i) => (
                      <div
                        key={i}
                        className="flex items-center gap-2 rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2"
                      >
                        <span className="flex-1 truncate text-[11px] font-medium text-fg">
                          {ct.name}
                        </span>
                        <span className="font-mono text-[9px] text-subtle">
                          {ct.type}
                        </span>
                        <button
                          type="button"
                          onClick={async () => {
                            setCustomResult("");
                            try {
                              const res =
                                ct.type === "shell"
                                  ? await runShellCommand(ct.command)
                                  : await httpRequest("GET", ct.url, "");
                              setCustomResult(res);
                            } catch (e) {
                              setCustomResult(
                                e instanceof Error ? e.message : String(e),
                              );
                            }
                          }}
                          className="btn h-7 text-[10px]"
                        >
                          Run
                        </button>
                      </div>
                    ))}
                    {customResult && (
                      <pre className="mt-2 max-h-40 overflow-auto rounded border border-line bg-[var(--bg-base)] p-2 font-mono text-[10px] text-muted whitespace-pre-wrap">
                        {customResult}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {selectedServer && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]">
            <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
              <span className="text-[12.5px] font-medium text-fg">
                Tools — {selectedServer.name}
              </span>
              <button
                type="button"
                onClick={() => setSelectedServer(null)}
                className="btn btn-ghost h-7 text-[10.5px]"
              >
                Close
              </button>
            </div>

            <div className="mt-6">
              <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
                Popular servers
              </p>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {[
                  { name: "filesystem", command: "npx", args: "-y @modelcontextprotocol/server-filesystem ." },
                  { name: "fetch", command: "uvx", args: "mcp-server-fetch" },
                ].map((s) => (
                  <div
                    key={s.name}
                    className="flex items-center justify-between rounded-lg border border-line bg-[var(--bg-surface)] px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="text-[11.5px] font-medium text-fg">{s.name}</p>
                      <p className="truncate font-mono text-[9px] text-subtle">
                        {s.command} {s.args}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        addMcpServer({
                          name: s.name,
                          transport: "stdio",
                          command: s.command,
                          args: s.args,
                          url: "",
                        });
                        pushToast({ title: `${s.name} added`, tone: "success" });
                      }}
                      className="btn h-7 text-[10px]"
                    >
                      Add
                    </button>
                  </div>
                ))}
              </div>
            </div>
            <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-4">
              {(toolsMap[selectedServer.id] ?? []).length === 0 ? (
                <p className="text-[11px] text-muted">No tools found.</p>
              ) : (
                <div className="space-y-3">
                  {(toolsMap[selectedServer.id] ?? []).map((tool) => (
                    <div
                      key={tool.name}
                      className="rounded-lg border border-line bg-[var(--bg-base)] p-3"
                    >
                      <p className="text-[12px] font-medium text-fg">
                        {tool.name}
                      </p>
                      {tool.description && (
                        <p className="mt-0.5 text-[10.5px] text-subtle">
                          {tool.description}
                        </p>
                      )}
                      <textarea
                        value={toolArgs}
                        onChange={(e) => setToolArgs(e.target.value)}
                        rows={2}
                        className="mt-2 w-full rounded border border-line bg-[var(--bg-surface)] px-2 py-1 font-mono text-[10px] text-fg outline-none"
                      />
                      <button
                        type="button"
                        disabled={invoking}
                        onClick={async () => {
                          setInvoking(true);
                          setToolResult("");
                          try {
                            const res =
                              selectedServer.transport === "stdio"
                                ? await mcpCallToolStdio(
                                    selectedServer.command,
                                    selectedServer.args,
                                    tool.name,
                                    toolArgs,
                                  )
                                : await mcpCallToolHttp(
                                    selectedServer.url,
                                    tool.name,
                                    toolArgs,
                                  );
                            setToolResult(res);
                            useApp.getState().addAuditEntry({
                              server: selectedServer.name,
                              tool: tool.name,
                              args: toolArgs,
                              result: res.slice(0, 2000),
                            });
                          } catch (e) {
                            setToolResult(
                              e instanceof Error ? e.message : String(e),
                            );
                          } finally {
                            setInvoking(false);
                          }
                        }}
                        className="btn btn-primary mt-2 h-7 text-[10.5px]"
                      >
                        {invoking ? "Invoking…" : "Invoke"}
                      </button>
                      {toolResult && (
                        <pre className="mt-2 max-h-40 overflow-auto rounded border border-line bg-[var(--bg-surface)] p-2 font-mono text-[10px] text-muted whitespace-pre-wrap">
                          {toolResult}
                        </pre>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {(resourcesMap[selectedServer.id] ?? []).length > 0 && (
                <div className="mt-4">
                  <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
                    Resources
                  </p>
                  <div className="space-y-1">
                    {(resourcesMap[selectedServer.id] ?? []).map((r) => (
                      <div
                        key={r.uri}
                        className="rounded border border-line bg-[var(--bg-base)] px-2.5 py-1.5"
                      >
                        <p className="text-[11px] font-medium text-fg">
                          {r.name ?? r.uri}
                        </p>
                        {r.description && (
                          <p className="text-[9.5px] text-subtle">
                            {r.description}
                          </p>
                        )}
                        <p className="truncate font-mono text-[9px] text-subtle">
                          {r.uri}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Delete MCP server?"
        description={
          deleteTarget
            ? `"${deleteTarget.name}" will be removed from your configuration.`
            : ""
        }
        confirmLabel="Delete server"
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => {
          if (!deleteTarget) return;
          deleteMcpServer(deleteTarget.id);
          pushToast({ title: "MCP server deleted" });
          setDeleteTarget(null);
        }}
      />
    </div>
  );
}
