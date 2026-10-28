import {
  Braces,
  Check,
  ChevronRight,
  Code2,
  File,
  FileCode2,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  HardDrive,
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
import type { ProjectFile } from "@/types";

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

  const project = projects.find((item) => item.id === activeProjectId);
  const [selectedPath, setSelectedPath] = useState("");
  const [draft, setDraft] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [savedTick, setSavedTick] = useState(0);
  const lastProject = useRef<string | undefined>(undefined);

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

  const selectedFile = project?.files.find(
    (file) => file.path === selectedPath,
  );
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
              <div className="flex h-full min-w-0 items-center gap-1.5 border-r border-line px-2.5 text-[11px] text-fg">
                <FileGlyph path={selectedFile.path} size={11} />
                <span className="truncate">{selectedFile.path.split("/").pop()}</span>
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
    </div>
  );
}
