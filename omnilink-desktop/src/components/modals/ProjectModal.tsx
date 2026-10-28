import { useEffect, useState, type FormEvent } from "react";

import { Dialog } from "@/components/ui/Dialog";
import { useApp } from "@/store/app";

export function ProjectModal({ open }: { open: boolean }) {
  const addProject = useApp((state) => state.addProject);
  const closeModal = useApp((state) => state.closeModal);
  const pushToast = useApp((state) => state.pushToast);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setName("");
    setDescription("");
    setError("");
  }, [open]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Give the project a name.");
      return;
    }
    if (trimmed.length > 48) {
      setError("Keep the name under 48 characters.");
      return;
    }
    if (!/^[A-Za-z0-9._ -]+$/.test(trimmed)) {
      setError("Use letters, numbers, spaces, dots, dashes, or underscores.");
      return;
    }
    addProject(trimmed, description.trim());
    pushToast({
      title: "Project created",
      description: `${trimmed} is ready in your local workspace.`,
      tone: "success",
    });
  };

  return (
    <Dialog
      open={open}
      onClose={closeModal}
      title="New coding project"
      description="Create a local project with a starter README and source file."
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={closeModal}>
            Cancel
          </button>
          <button
            type="submit"
            form="project-form"
            className="btn btn-primary"
            disabled={!name.trim()}
          >
            Create project
          </button>
        </>
      }
    >
      <form id="project-form" onSubmit={submit} className="space-y-4 pt-1">
        <div>
          <label
            htmlFor="project-name"
            className="mb-1.5 block text-[11px] font-medium text-muted"
          >
            Project name
          </label>
          <input
            id="project-name"
            data-autofocus
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setError("");
            }}
            placeholder="my-new-project"
            className="field"
            spellCheck={false}
            autoComplete="off"
          />
        </div>
        <div>
          <label
            htmlFor="project-description"
            className="mb-1.5 block text-[11px] font-medium text-muted"
          >
            Description <span className="text-subtle">optional</span>
          </label>
          <textarea
            id="project-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="What are you building?"
            className="field min-h-[72px]"
          />
        </div>
        {error && <p className="text-[11px] text-danger">{error}</p>}
        <div className="rounded-lg border border-line bg-[var(--bg-base)] px-3 py-2.5 text-[10.5px] leading-relaxed text-subtle">
          The project starts with <span className="font-mono text-muted">README.md</span> and{" "}
          <span className="font-mono text-muted">src/index.ts</span>. Files are
          stored locally and can be edited immediately.
        </div>
      </form>
    </Dialog>
  );
}
