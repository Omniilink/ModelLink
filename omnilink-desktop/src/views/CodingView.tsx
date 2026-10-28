import {
  Braces,
  Camera,
  Check,
  ChevronRight,
  Code2,
  Eye,
  File,
  FileCode2,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  HardDrive,
  History,
  Save,
  Share2,
  Trash2,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { CodeEditor } from "@/components/editor/CodeEditor";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { IconButton } from "@/components/ui/IconButton";
import { useResolvedTheme } from "@/lib/theme";
import {
  cn,
  formatRelative,
  languageForPath,
} from "@/lib/utils";
import { useApp } from "@/store/app";
import type { ProjectFile, ModelProvider } from "@/types";
import { isModelConfigured, streamChat } from "@/lib/models";

interface TreeDir {
  type: "dir";
  name: string;
  path: string;
  children: TreeNode[];
}

interface TreeFile {
  type: "file";
  name: string;
  path: string;
}

type TreeNode = TreeDir | TreeFile;

function buildTree(files: ProjectFile[]): TreeNode[] {
  const root: TreeNode[] = [];

  for (const file of files) {
    const parts = file.path.split("/");
    let level = root;
    let accumulated = "";
    parts.forEach((part, index) => {
      accumulated = accumulated ? `${accumulated}/${part}` : part;
      if (index === parts.length - 1) {
        level.push({ type: "file", name: part, path: file.path });
        return;
      }
      let directory = level.find(
        (node): node is TreeDir => node.type === "dir" && node.name === part,
      );
      if (!directory) {
        directory = {
          type: "dir",
          name: part,
          path: accumulated,
          children: [],
        };
        level.push(directory);
      }
      level = directory.children;
    });
  }

  const sortLevel = (nodes: TreeNode[]): TreeNode[] => {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const node of nodes) {
      if (node.type === "dir") sortLevel(node.children);
    }
    return nodes;
  };

  return sortLevel(root);
}

function collectDirs(nodes: TreeNode[], into = new Set<string>()) {
  for (const node of nodes) {
    if (node.type === "dir") {
      into.add(node.path);
      collectDirs(node.children, into);
    }
  }
  return into;
}

function FileGlyph({ path, size = 12 }: { path: string; size?: number }) {
  const ext = path.split(".").pop()?.toLowerCase();
  if (ext === "json") return <Braces size={size} />;
  if (ext === "md" || ext === "txt") return <FileText size={size} />;
  if (
    ext === "ts" ||
    ext === "tsx" ||
    ext === "js" ||
    ext === "jsx" ||
    ext === "rs" ||
    ext === "py"
  ) {
    return <FileCode2 size={size} />;
  }
  return <File size={size} />;
}

function TreeBranch({
  nodes,
  depth,
  expanded,
  toggleDir,
  selectedPath,
  onSelect,
}: {
  nodes: TreeNode[];
  depth: number;
  expanded: Set<string>;
  toggleDir: (path: string) => void;
  selectedPath: string;
  onSelect: (path: string) => void;
}) {
  return (
    <>
      {nodes.map((node) => {
        if (node.type === "dir") {
          const open = expanded.has(node.path);
          return (
            <div key={node.path}>
              <button
                type="button"
                onClick={() => toggleDir(node.path)}
                className="flex h-[26px] w-full items-center gap-1.5 pr-2 text-left text-[11px] text-muted transition-colors hover:bg-[var(--bg-hover)] hover:text-fg"
                style={{ paddingLeft: 8 + depth * 12 }}
              >
                <ChevronRight
                  size={10}
                  className={cn(
                    "flex-none text-subtle transition-transform",
                    open && "rotate-90",
                  )}
                />
                {open ? (
                  <FolderOpen size={12} className="flex-none text-subtle" />
                ) : (
                  <Folder size={12} className="flex-none text-subtle" />
                )}
                <span className="truncate">{node.name}</span>
              </button>
              {open && (
                <TreeBranch
                  nodes={node.children}
                  depth={depth + 1}
                  expanded={expanded}
                  toggleDir={toggleDir}
                  selectedPath={selectedPath}
                  onSelect={onSelect}
                />
              )}
            </div>
          );
        }

        const active = node.path === selectedPath;
        return (
          <button
            key={node.path}
            type="button"
            title={node.path}
            onClick={() => onSelect(node.path)}
            className={cn(
              "relative flex h-[26px] w-full items-center gap-1.5 pr-2 text-left text-[11px] transition-colors",
              active
                ? "bg-[var(--bg-active)] text-fg"
                : "text-muted hover:bg-[var(--bg-hover)] hover:text-fg",
            )}
            style={{ paddingLeft: 8 + depth * 12 + 13 }}
          >
            {active && (
              <span className="absolute left-0 top-1/2 h-3.5 w-[2px] -translate-y-1/2 rounded-full bg-accent" />
            )}
            <span
              className={cn(
                "flex-none",
                active ? "text-accent-text" : "text-subtle",
              )}
            >
              <FileGlyph path={node.path} />
            </span>
            <span className="truncate">{node.name}</span>
          </button>
        );
      })}
    </>
  );
}

export function CodingView() {
  const projects = useApp((state) => state.projects);
  const activeProjectId = useApp((state) => state.activeProjectId);
  const updateProjectFile = useApp((state) => state.updateProjectFile);
  const deleteProject = useApp((state) => state.deleteProject);
  const openModal = useApp((state) => state.openModal);
  const pushToast = useApp((state) => state.pushToast);
  const resolvedTheme = useResolvedTheme();
  const projectVersions = useApp((state) => state.projectVersions);
  const createSnapshot = useApp((state) => state.createSnapshot);
  const deleteSnapshot = useApp((state) => state.deleteSnapshot);
  const restoreSnapshot = useApp((state) => state.restoreSnapshot);

  const project = projects.find((item) => item.id === activeProjectId);
  const [selectedPath, setSelectedPath] = useState("");
  const [draft, setDraft] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [savedTick, setSavedTick] = useState(0);
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const [snapshotName, setSnapshotName] = useState("");
  const lastProject = useRef<string | undefined>(undefined);

  const modelProvider = useApp((state) => state.modelProvider);
  const modelBaseUrl = useApp((state) => state.modelBaseUrl);
  const modelApiKey = useApp((state) => state.modelApiKey);
  const modelName = useApp((state) => state.modelName);
  const providers = useApp((state) => state.providers);
  const activeProviderId = useApp((state) => state.activeProviderId);

  const runAgent = async () => {
    if (!project || agentRunning || !agentTask.trim()) return;
    const activeProvider = providers.find((p) => p.id === activeProviderId);
    const config = activeProvider
      ? {
          provider: (activeProvider.baseUrl.includes("127.0.0.1") ||
          activeProvider.baseUrl.includes("localhost")
            ? "ollama"
            : "custom") as ModelProvider,
          baseUrl: activeProvider.baseUrl,
          apiKey: "",
          model: activeProvider.model,
        }
      : {
          provider: modelProvider,
          baseUrl: modelBaseUrl,
          apiKey: modelApiKey,
          model: modelName,
        };
    if (!isModelConfigured(config)) {
      pushToast({ title: "Connect a model first", tone: "danger" });
      return;
    }
    setAgentRunning(true);
    setAgentLog("");
    let accumulated = "";
    const context = project.files
      .map((f) => `### ${f.path}\n${f.content}`)
      .join("\n\n");
    const messages = [
      { role: "system" as const, content: "You are a coding agent. When you want to edit a file, respond with a JSON block: {\"edits\":[{\"path\":\"...\",\"content\":\"...\"}]}. Otherwise explain your plan in prose. End with DONE when finished." , createdAt: Date.now(), id: "sys" },
      { role: "user" as const, content: `Project: ${project.name}\n\nFiles:\n${context}\n\nTask: ${agentTask}`, createdAt: Date.now(), id: "u1" },
    ];
    try {
      await streamChat(config, messages as any, (token) => {
        accumulated += token;
        setAgentLog(accumulated);
      });
      // Try to parse edits.
      const match = accumulated.match(/\{[\s\S]*"edits"[\s\S]*\}/);
      if (match) {
        try {
          const parsed = JSON.parse(match[0]) as { edits: { path: string; content: string }[] };
          if (parsed.edits?.length) {
            setAgentPreview(parsed.edits[0]);
          }
        } catch {
          // ignore parse errors
        }
      }
    } catch (error) {
      setAgentLog((prev) => prev + `\n\n⚠ ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setAgentRunning(false);
    }
  };

  const [agentMode, setAgentMode] = useState(false);
  const [agentTask, setAgentTask] = useState("");
  const [agentLog, setAgentLog] = useState("");
  const [agentRunning, setAgentRunning] = useState(false);
  const [agentPreview, setAgentPreview] = useState<{
    path: string;
    content: string;
  } | null>(null);

  const tree = useMemo(
    () => buildTree(project?.files ?? []),
    [project?.files],
  );

  useEffect(() => {
    if (!project || lastProject.current === project.id) return;
    lastProject.current = project.id;
    const initial =
      project.files.find((file) => file.path === "README.md") ??
      project.files[0];
    setSelectedPath(initial?.path ?? "");
    setDraft(initial?.content ?? "");
    setExpanded(collectDirs(buildTree(project.files)));
  }, [project]);

  useEffect(() => {
    if (!project) {
      lastProject.current = undefined;
    }
  }, [project]);

  const [previewOpen, setPreviewOpen] = useState(false);

  const selectedFile = project?.files.find(
    (file) => file.path === selectedPath,
  );

  const scanDangerous = (content: string): string[] => {
    const warnings: string[] = [];
    const patterns: [RegExp, string][] = [
      [/eval\s*\(/, "eval()"],
      [/new\s+Function\s*\(/, "new Function()"],
      [/child_process/, "child_process"],
      [/require\s*\(\s*['"]fs['"]\s*\)/, "fs module"],
      [/process\.exit/, "process.exit"],
      [/<script[^>]*src=/i, "external script"],
    ];
    for (const [re, label] of patterns) {
      if (re.test(content)) warnings.push(label);
    }
    return warnings;
  };
  const dirty = Boolean(selectedFile && draft !== selectedFile.content);

  const saveNow = useCallback(() => {
    if (!project || !selectedPath) return;
    const file = project.files.find((item) => item.path === selectedPath);
    if (!file || file.content === draft) return;
    updateProjectFile(project.id, selectedPath, draft);
    setSavedTick(Date.now());
  }, [draft, project, selectedPath, updateProjectFile]);

  useEffect(() => {
    if (!project || !selectedFile || !dirty) return;
    const timer = window.setTimeout(() => {
      updateProjectFile(project.id, selectedPath, draft);
      setSavedTick(Date.now());
    }, 850);
    return () => window.clearTimeout(timer);
  }, [
    dirty,
    draft,
    project,
    selectedFile,
    selectedPath,
    updateProjectFile,
  ]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveNow();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [saveNow]);

  useEffect(() => {
    if (!dirty) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("[data-code-editor]")) return;
      saveNow();
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    return () => window.removeEventListener("pointerdown", onPointerDown, true);
  }, [dirty, saveNow]);

  const selectFile = (path: string) => {
    if (path === selectedPath) return;
    if (project && dirty) saveNow();
    const next = project?.files.find((file) => file.path === path);
    if (!next) return;
    setSelectedPath(path);
    setDraft(next.content);
  };

  const toggleDir = (path: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  if (!project) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-8 text-center">
        <div className="relative">
          <div className="absolute inset-0 rounded-2xl bg-accent opacity-20 blur-2xl" />
          <div className="relative flex h-14 w-14 items-center justify-center rounded-2xl border border-line bg-[var(--bg-elevated)] text-accent-text shadow-xl">
            <Code2 size={24} />
          </div>
        </div>
        <h1 className="mt-5 text-[22px] font-semibold tracking-[-0.025em] text-fg">
          Start a coding project
        </h1>
        <p className="mt-2 max-w-[360px] text-[12.5px] leading-relaxed text-muted">
          Create a local project with a starter README and source file, then
          edit it in the built-in editor.
        </p>
        <button
          type="button"
          className="btn btn-primary mt-5"
          onClick={() => openModal({ type: "project-new" })}
        >
          <FolderPlus size={14} />
          New project
        </button>
        <p className="mt-3 text-[10.5px] text-subtle">
          Projects live with your local workspace.
        </p>
      </div>
    );
  }

  const lineCount = draft ? draft.split("\n").length : 0;

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[52px] flex-none items-center gap-3 border-b border-line px-5">
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-muted">
          <Code2 size={14} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-[13px] font-semibold tracking-[-0.01em] text-fg">
              {project.name}
            </h1>
            {project.sample && (
              <span className="rounded border border-line px-1.5 py-px text-[8.5px] font-medium uppercase tracking-wide text-subtle">
                sample
              </span>
            )}
            <span className="hidden items-center gap-1 rounded border border-line px-1.5 py-px text-[8.5px] font-medium uppercase tracking-wide text-subtle sm:flex">
              <HardDrive size={9} />
              local
            </span>
          </div>
          <p className="mt-0.5 truncate text-[10px] text-subtle">
            {project.files.length} files · edited{" "}
            {formatRelative(project.updatedAt)}
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          <span
            className={cn(
              "flex items-center gap-1 text-[10px] transition-colors",
              dirty ? "text-warning" : "text-subtle",
            )}
          >
            {dirty ? (
              <>
                <span className="h-1.5 w-1.5 rounded-full bg-warning" />
                Unsaved
              </>
            ) : (
              <>
                <Check size={11} className="text-success" />
                Saved
              </>
            )}
          </span>
          <IconButton
            label="Toggle agent"
            onClick={() => setAgentMode((v) => !v)}
            className="h-7 w-7"
          >
            <Braces size={13} />
          </IconButton>
          {selectedFile?.path.toLowerCase().endsWith(".html") && (
            <IconButton
              label="Preview file"
              onClick={() => {
                const warnings = scanDangerous(selectedFile.content);
                if (warnings.length > 0) {
                  const ok = window.confirm(
                    `Safety scan found: ${warnings.join(", ")}. Preview anyway?`,
                  );
                  if (!ok) return;
                }
                setPreviewOpen(true);
              }}
              className="h-7 w-7"
            >
              <Eye size={13} />
            </IconButton>
          )}
          <IconButton
            label="Create snapshot"
            onClick={() => {
              const id = createSnapshot(project.id, snapshotName);
              if (id) {
                setSnapshotName("");
                pushToast({ title: "Snapshot created", tone: "success" });
              }
            }}
            className="h-7 w-7"
          >
            <Camera size={13} />
          </IconButton>
          <IconButton
            label="Versions"
            onClick={() => setSnapshotOpen(true)}
            className="h-7 w-7"
          >
            <History size={13} />
          </IconButton>
          <IconButton
            label="Share project"
            onClick={() => {
              const blob = new Blob([JSON.stringify(project, null, 2)], {
                type: "application/json",
              });
              const url = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = url;
              a.download = `${project.name}.omnilink.json`;
              a.click();
              URL.revokeObjectURL(url);
              pushToast({ title: "Project exported", tone: "success" });
            }}
            className="h-7 w-7"
          >
            <Share2 size={13} />
          </IconButton>
          <IconButton
            label="Delete project"
            onClick={() => setConfirmDelete(true)}
            className="ml-1"
          >
            <Trash2 size={14} />
          </IconButton>
        </div>
      </div>

      {agentMode && (
        <div className="flex min-h-0 flex-1 flex-col gap-3 border-b border-line px-5 py-4">
          <textarea
            value={agentTask}
            onChange={(e) => setAgentTask(e.target.value)}
            placeholder="Describe the change you want the agent to make…"
            className="min-h-[80px] w-full resize-none rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2 text-[12.5px] text-fg outline-none"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={agentRunning || !agentTask.trim()}
              onClick={() => void runAgent()}
              className="btn btn-primary h-8 text-[10.5px]"
            >
              {agentRunning ? "Running…" : "Run agent"}
            </button>
            <button
              type="button"
              onClick={() => setAgentMode(false)}
              className="btn btn-ghost h-8 text-[10.5px]"
            >
              Close
            </button>
          </div>
          {agentLog && (
            <pre className="scroll-thin max-h-64 overflow-auto rounded-lg border border-line bg-[var(--bg-base)] p-3 font-mono text-[10.5px] text-muted whitespace-pre-wrap">
              {agentLog}
            </pre>
          )}
          {agentPreview && (
            <div className="rounded-lg border border-line bg-[var(--bg-base)] p-3">
              <p className="mb-2 text-[11px] font-medium text-fg">
                Proposed edit: {agentPreview.path}
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    if (project) {
                      updateProjectFile(project.id, agentPreview.path, agentPreview.content);
                      pushToast({ title: "Edit applied", tone: "success" });
                    }
                    setAgentPreview(null);
                  }}
                  className="btn btn-primary h-7 text-[10.5px]"
                >
                  Apply
                </button>
                <button
                  type="button"
                  onClick={() => setAgentPreview(null)}
                  className="btn btn-ghost h-7 text-[10.5px]"
                >
                  Discard
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[208px] flex-none flex-col border-r border-line bg-[color-mix(in_srgb,var(--bg-surface)_60%,transparent)]">
          <div className="flex h-9 flex-none items-center justify-between border-b border-line px-3">
            <span className="text-[9px] font-semibold uppercase tracking-[0.15em] text-subtle">
              Files
            </span>
            <span className="text-[9.5px] tabular-nums text-subtle">
              {project.files.length}
            </span>
          </div>
          <div className="scroll-thin min-h-0 flex-1 overflow-auto py-1.5">
            <TreeBranch
              nodes={tree}
              depth={0}
              expanded={expanded}
              toggleDir={toggleDir}
              selectedPath={selectedPath}
              onSelect={selectFile}
            />
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col" data-code-editor>
          <div className="flex h-9 flex-none items-center gap-2 border-b border-line pl-1 pr-2.5">
            {selectedFile ? (
              <div className="flex h-full min-w-0 items-center gap-1 border-r border-line px-2.5 text-[11px] text-fg">
                {selectedFile.path.split("/").map((part, i, arr) => (
                  <span key={i} className="flex items-center gap-1">
                    {i > 0 && <ChevronRight size={10} className="text-subtle" />}
                    <span className={i === arr.length - 1 ? "text-fg" : "text-muted"}>
                      {part}
                    </span>
                  </span>
                ))}
                {dirty && (
                  <span className="ml-1 h-1.5 w-1.5 rounded-full bg-warning" />
                )}
              </div>
            ) : (
              <span className="px-2 text-[10.5px] text-subtle">
                No file selected
              </span>
            )}
            <div className="flex-1" />
            <span
              className="hidden text-[9.5px] text-subtle md:inline"
              key={savedTick}
            >
              {dirty ? "" : selectedFile ? "Autosaved locally" : ""}
            </span>
            <button
              type="button"
              onClick={saveNow}
              disabled={!dirty}
              className={cn(
                "btn h-6 gap-1 px-2 text-[10px]",
                dirty ? "btn-primary" : "btn-ghost",
              )}
            >
              <Save size={11} />
              Save
            </button>
          </div>

          <div className="min-h-0 flex-1">
            {selectedFile ? (
              <CodeEditor
                key={`${project.id}:${selectedFile.path}`}
                value={draft}
                language={languageForPath(selectedFile.path)}
                path={`${project.id}/${selectedFile.path}`}
                theme={resolvedTheme}
                onChange={setDraft}
                onSave={saveNow}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-[11px] text-subtle">
                Choose a file from the tree.
              </div>
            )}
          </div>

          <div className="flex h-6 flex-none items-center gap-3 border-t border-line bg-[var(--bg-surface)] px-3 text-[9.5px] text-subtle">
            <span className="truncate font-mono">{selectedFile?.path}</span>
            <div className="flex-1" />
            {selectedFile && (
              <>
                <span>{languageForPath(selectedFile.path)}</span>
                <span>{lineCount} lines</span>
                <span>UTF-8</span>
                <span>LF</span>
              </>
            )}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete project?"
        description={`"${project.name}" and its ${project.files.length} files will be removed from this device.`}
        confirmLabel="Delete project"
        onClose={() => setConfirmDelete(false)}
        onConfirm={() => {
          deleteProject(project.id);
          pushToast({ title: "Project deleted" });
        }}
      />

      {snapshotOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]">
            <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
              <span className="text-[12.5px] font-medium text-fg">
                Snapshots — {project.name}
              </span>
              <button
                type="button"
                onClick={() => setSnapshotOpen(false)}
                className="btn btn-ghost h-7 text-[10.5px]"
              >
                Close
              </button>
            </div>
            <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-4">
              {projectVersions.filter((v) => v.projectId === project.id).length === 0 ? (
                <p className="text-[11px] text-muted">No snapshots yet.</p>
              ) : (
                <div className="space-y-2">
                  {projectVersions
                    .filter((v) => v.projectId === project.id)
                    .map((v) => (
                      <div
                        key={v.id}
                        className="flex items-center gap-3 rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-[12px] font-medium text-fg">
                            {v.name}
                          </p>
                          <p className="text-[9.5px] text-subtle">
                            {v.files.length} files ·{" "}
                            {new Date(v.createdAt).toLocaleString()}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => {
                            restoreSnapshot(v.id);
                            pushToast({ title: "Snapshot restored", tone: "success" });
                            setSnapshotOpen(false);
                          }}
                          className="btn h-7 text-[10px]"
                        >
                          Restore
                        </button>
                        <button
                          type="button"
                          onClick={() => deleteSnapshot(v.id)}
                          className="btn btn-danger h-7 text-[10px]"
                        >
                          Delete
                        </button>
                      </div>
                    ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {previewOpen && selectedFile && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
          <div className="flex h-[80vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]">
            <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
              <span className="truncate font-mono text-[11px] text-muted">
                {selectedFile.path}
              </span>
              <button
                type="button"
                onClick={() => setPreviewOpen(false)}
                className="btn btn-ghost h-7 text-[10.5px]"
              >
                Close
              </button>
            </div>
            <iframe
              title="Preview"
              srcDoc={selectedFile.content}
              sandbox="allow-scripts"
              className="min-h-0 flex-1 border-0 bg-white"
            />
          </div>
        </div>
      )}
    </div>
  );
}
