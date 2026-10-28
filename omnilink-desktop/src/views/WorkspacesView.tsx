import { FolderKanban, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { IconButton } from "@/components/ui/IconButton";
import { useApp } from "@/store/app";

export function WorkspacesView() {
  const workspaces = useApp((state) => state.workspaces);
  const projects = useApp((state) => state.projects);
  const activeWorkspaceId = useApp((state) => state.activeWorkspaceId);
  const addWorkspace = useApp((state) => state.addWorkspace);
  const deleteWorkspace = useApp((state) => state.deleteWorkspace);
  const setActiveWorkspace = useApp((state) => state.setActiveWorkspace);
  const renameWorkspace = useApp((state) => state.renameWorkspace);
  const pushToast = useApp((state) => state.pushToast);
  const [name, setName] = useState("");

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[52px] flex-none items-center gap-3 border-b border-line px-5">
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-muted">
          <FolderKanban size={14} />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-[13px] font-semibold tracking-[-0.01em] text-fg">
            Workspaces
          </h1>
          <p className="mt-0.5 text-[10px] text-subtle">
            {workspaces.length} workspace{workspaces.length === 1 ? "" : "s"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="New workspace name"
            className="field h-8 w-44 text-[11px]"
          />
          <button
            type="button"
            onClick={() => {
              if (!name.trim()) return;
              addWorkspace(name.trim());
              setName("");
              pushToast({ title: "Workspace created", tone: "success" });
            }}
            className="btn btn-primary h-8 text-[10.5px]"
          >
            <Plus size={13} />
            Create
          </button>
        </div>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {workspaces.map((workspace) => {
            const count = projects.filter(
              (p) => (p.workspaceId ?? "default") === workspace.id,
            ).length;
            const active = workspace.id === activeWorkspaceId;
            return (
              <div
                key={workspace.id}
                className={`rounded-xl border p-4 transition-colors ${
                  active
                    ? "border-[var(--accent-border)] bg-[var(--accent-softer)]"
                    : "border-line bg-[var(--bg-surface)]"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <h2 className="truncate text-[13px] font-semibold text-fg">
                    {workspace.name}
                  </h2>
                  {workspace.id !== "default" && (
                    <IconButton
                      label="Delete workspace"
                      onClick={() => {
                        deleteWorkspace(workspace.id);
                        pushToast({ title: "Workspace deleted" });
                      }}
                      className="h-6 w-6 hover:text-danger"
                    >
                      <Trash2 size={12} />
                    </IconButton>
                  )}
                </div>
                <p className="mt-1 text-[10.5px] text-subtle">
                  {count} project{count === 1 ? "" : "s"}
                </p>
                <div className="mt-3 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setActiveWorkspace(workspace.id);
                      pushToast({
                        title: `Switched to ${workspace.name}`,
                        tone: "success",
                      });
                    }}
                    className="btn h-7 text-[10.5px]"
                  >
                    {active ? "Active" : "Switch"}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const newName = window.prompt("Rename workspace", workspace.name);
                      if (newName) renameWorkspace(workspace.id, newName);
                    }}
                    className="btn btn-ghost h-7 text-[10.5px]"
                  >
                    Rename
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
