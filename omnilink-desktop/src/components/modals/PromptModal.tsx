import { useEffect, useState, type FormEvent } from "react";

import { Dialog } from "@/components/ui/Dialog";
import { useApp } from "@/store/app";

export function PromptModal({
  open,
  promptId,
}: {
  open: boolean;
  promptId?: string;
}) {
  const prompts = useApp((state) => state.prompts);
  const addPrompt = useApp((state) => state.addPrompt);
  const updatePrompt = useApp((state) => state.updatePrompt);
  const closeModal = useApp((state) => state.closeModal);
  const pushToast = useApp((state) => state.pushToast);

  const existing = promptId
    ? prompts.find((prompt) => prompt.id === promptId)
    : undefined;

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [tags, setTags] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setTitle(existing?.title ?? "");
    setDescription(existing?.description ?? "");
    setBody(existing?.body ?? "");
    setTags(existing?.tags.join(", ") ?? "");
    setError("");
    // Only reset when the dialog opens for a specific prompt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, promptId]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const cleanTitle = title.trim();
    const cleanBody = body.trim();
    if (!cleanTitle) {
      setError("Add a title so you can find this prompt later.");
      return;
    }
    if (!cleanBody) {
      setError("The prompt body cannot be empty.");
      return;
    }
    const parsedTags = [
      ...new Set(
        tags
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean)
          .slice(0, 8),
      ),
    ];
    const payload = {
      title: cleanTitle.slice(0, 80),
      description: description.trim().slice(0, 220),
      body: cleanBody,
      tags: parsedTags,
    };
    if (existing) {
      updatePrompt(existing.id, payload);
      pushToast({ title: "Prompt updated", tone: "success" });
    } else {
      addPrompt(payload);
      pushToast({
        title: "Prompt saved",
        description: "It is ready in your library.",
        tone: "success",
      });
    }
  };

  return (
    <Dialog
      open={open}
      onClose={closeModal}
      title={existing ? "Edit prompt" : "New prompt"}
      description={
        existing
          ? "Refine the instruction. Changes are saved locally."
          : "Save a reusable instruction for chats and coding sessions."
      }
      className="max-w-[580px]"
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={closeModal}>
            Cancel
          </button>
          <button
            type="submit"
            form="prompt-form"
            className="btn btn-primary"
            disabled={!title.trim() || !body.trim()}
          >
            {existing ? "Save changes" : "Save prompt"}
          </button>
        </>
      }
    >
      <form id="prompt-form" onSubmit={submit} className="space-y-4 pt-1">
        <div className="grid grid-cols-[1fr] gap-4">
          <div>
            <label
              htmlFor="prompt-title"
              className="mb-1.5 block text-[11px] font-medium text-muted"
            >
              Title
            </label>
            <input
              id="prompt-title"
              data-autofocus
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
                setError("");
              }}
              placeholder="Senior code review"
              className="field"
            />
          </div>
          <div>
            <label
              htmlFor="prompt-description"
              className="mb-1.5 block text-[11px] font-medium text-muted"
            >
              Description <span className="text-subtle">optional</span>
            </label>
            <input
              id="prompt-description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What this prompt is for"
              className="field"
            />
          </div>
        </div>
        <div>
          <label
            htmlFor="prompt-body"
            className="mb-1.5 block text-[11px] font-medium text-muted"
          >
            Prompt
          </label>
          <textarea
            id="prompt-body"
            value={body}
            onChange={(event) => {
              setBody(event.target.value);
              setError("");
            }}
            placeholder="Write the instruction exactly as you want to use it…"
            className="field min-h-[168px] font-mono text-[11.5px] leading-relaxed"
          />
          <div className="mt-1.5 flex justify-between text-[10px] text-subtle">
            <span>Markdown is fine. Keep it focused.</span>
            <span className="tabular-nums">{body.length} chars</span>
          </div>
        </div>
        <div>
          <label
            htmlFor="prompt-tags"
            className="mb-1.5 block text-[11px] font-medium text-muted"
          >
            Tags <span className="text-subtle">comma separated</span>
          </label>
          <input
            id="prompt-tags"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="Coding, Review"
            className="field"
          />
        </div>
        {error && <p className="text-[11px] text-danger">{error}</p>}
      </form>
    </Dialog>
  );
}
