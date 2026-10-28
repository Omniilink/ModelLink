import { useEffect, useState, type FormEvent } from "react";

import { Dialog } from "@/components/ui/Dialog";
import { Segmented } from "@/components/ui/Segmented";
import { useApp } from "@/store/app";
import type { McpTransport } from "@/types";

export function McpModal({
  open,
  serverId,
}: {
  open: boolean;
  serverId?: string;
}) {
  const servers = useApp((state) => state.mcpServers);
  const addMcpServer = useApp((state) => state.addMcpServer);
  const updateMcpServer = useApp((state) => state.updateMcpServer);
  const closeModal = useApp((state) => state.closeModal);
  const pushToast = useApp((state) => state.pushToast);

  const existing = serverId
    ? servers.find((server) => server.id === serverId)
    : undefined;

  const [name, setName] = useState("");
  const [transport, setTransport] = useState<McpTransport>("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setName(existing?.name ?? "");
    setTransport(existing?.transport ?? "stdio");
    setCommand(existing?.command ?? "");
    setArgs(existing?.args ?? "");
    setUrl(existing?.url ?? "");
    setError("");
    // Only reset when the dialog opens for a specific server.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, serverId]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName) {
      setError("Give the server a recognizable name.");
      return;
    }
    if (transport === "stdio" && !command.trim()) {
      setError("A stdio server needs a command to launch.");
      return;
    }
    if (
      transport !== "stdio" &&
      !/^https?:\/\/.+/.test(url.trim())
    ) {
      setError("Enter a full http:// or https:// URL.");
      return;
    }

    const payload = {
      name: cleanName.slice(0, 60),
      transport,
      command: command.trim(),
      args: args.trim(),
      url: url.trim(),
    };
    if (existing) {
      updateMcpServer(existing.id, payload);
      pushToast({ title: "MCP server updated", tone: "success" });
    } else {
      addMcpServer(payload);
      pushToast({
        title: "MCP server added",
        description: `${payload.name} is configured locally.`,
        tone: "success",
      });
    }
  };

  return (
    <Dialog
      open={open}
      onClose={closeModal}
      title={existing ? "Edit MCP server" : "Add MCP server"}
      description="Register a local process or a remote endpoint for agent sessions."
      className="max-w-[560px]"
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={closeModal}>
            Cancel
          </button>
          <button
            type="submit"
            form="mcp-form"
            className="btn btn-primary"
            disabled={!name.trim()}
          >
            {existing ? "Save changes" : "Add server"}
          </button>
        </>
      }
    >
      <form id="mcp-form" onSubmit={submit} className="space-y-4 pt-1">
        <div>
          <label
            htmlFor="mcp-name"
            className="mb-1.5 block text-[11px] font-medium text-muted"
          >
            Name
          </label>
          <input
            id="mcp-name"
            data-autofocus
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setError("");
            }}
            placeholder="filesystem"
            className="field"
            spellCheck={false}
          />
        </div>

        <div>
          <span className="mb-1.5 block text-[11px] font-medium text-muted">
            Transport
          </span>
          <Segmented
            value={transport}
            onChange={(value) => {
              setTransport(value);
              setError("");
            }}
            label="MCP transport"
            options={[
              { value: "stdio", label: "stdio" },
              { value: "sse", label: "SSE" },
              { value: "http", label: "Streamable HTTP" },
            ]}
          />
        </div>

        {transport === "stdio" ? (
          <div className="space-y-3">
            <div>
              <label
                htmlFor="mcp-command"
                className="mb-1.5 block text-[11px] font-medium text-muted"
              >
                Command
              </label>
              <input
                id="mcp-command"
                value={command}
                onChange={(event) => {
                  setCommand(event.target.value);
                  setError("");
                }}
                placeholder="npx"
                className="field font-mono text-[11.5px]"
                spellCheck={false}
              />
            </div>
            <div>
              <label
                htmlFor="mcp-args"
                className="mb-1.5 block text-[11px] font-medium text-muted"
              >
                Arguments <span className="text-subtle">optional</span>
              </label>
              <textarea
                id="mcp-args"
                value={args}
                onChange={(event) => setArgs(event.target.value)}
                placeholder={"-y @modelcontextprotocol/server-filesystem C:\\projects"}
                className="field min-h-[74px] font-mono text-[11px]"
                spellCheck={false}
              />
              <p className="mt-1.5 text-[10px] leading-relaxed text-subtle">
                Space or newline separated. Arguments are passed to the process
                without a shell.
              </p>
            </div>
          </div>
        ) : (
          <div>
            <label
              htmlFor="mcp-url"
              className="mb-1.5 block text-[11px] font-medium text-muted"
            >
              Endpoint URL
            </label>
            <input
              id="mcp-url"
              value={url}
              onChange={(event) => {
                setUrl(event.target.value);
                setError("");
              }}
              placeholder={
                transport === "sse"
                  ? "https://mcp.example.com/sse"
                  : "https://mcp.example.com/mcp"
              }
              className="field font-mono text-[11.5px]"
              spellCheck={false}
            />
            <p className="mt-1.5 text-[10px] leading-relaxed text-subtle">
              {transport === "sse"
                ? "The server keeps a Server-Sent Events stream open for messages."
                : "Uses the streamable HTTP transport with a single MCP endpoint."}
            </p>
          </div>
        )}

        {error && <p className="text-[11px] text-danger">{error}</p>}

        <div className="rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2.5 text-[10.5px] leading-relaxed text-subtle">
          Configuration is stored locally. Servers can be disabled from the
          list without deleting their settings.
        </div>
      </form>
    </Dialog>
  );
}
