import { AlertTriangle } from "lucide-react";

import { Dialog } from "@/components/ui/Dialog";

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Delete",
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => {
              onConfirm();
              onClose();
            }}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-xl border border-[color-mix(in_srgb,var(--danger)_26%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-3.5">
        <span className="mt-0.5 flex h-7 w-7 flex-none items-center justify-center rounded-full bg-[color-mix(in_srgb,var(--danger)_16%,transparent)] text-danger">
          <AlertTriangle size={14} />
        </span>
        <p className="text-[12px] leading-relaxed text-muted">
          This action cannot be undone. Make sure anything you need is exported
          or saved somewhere else first.
        </p>
      </div>
    </Dialog>
  );
}
