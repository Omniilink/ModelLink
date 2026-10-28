import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

import { useApp } from "@/store/app";
import { cn } from "@/lib/utils";

export function Toaster() {
  const toasts = useApp((state) => state.toasts);
  const dismissToast = useApp((state) => state.dismissToast);

  return (
    <div className="pointer-events-none fixed bottom-11 right-4 z-[130] flex w-[344px] flex-col gap-2">
      <AnimatePresence initial={false}>
        {toasts.map((toast) => {
          const Icon =
            toast.tone === "success"
              ? CheckCircle2
              : toast.tone === "danger"
                ? AlertTriangle
                : Info;
          return (
            <motion.div
              key={toast.id}
              layout
              initial={{ opacity: 0, y: 10, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 4, scale: 0.98 }}
              transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
              className="pointer-events-auto flex items-start gap-3 rounded-xl border border-line bg-overlay p-3.5 shadow-[0_18px_50px_-12px_rgba(0,0,0,0.6)]"
            >
              <Icon
                size={15}
                className={cn(
                  "mt-0.5 flex-none",
                  toast.tone === "success"
                    ? "text-success"
                    : toast.tone === "danger"
                      ? "text-danger"
                      : "text-accent-text",
                )}
              />
              <div className="min-w-0 flex-1">
                <p className="text-[12.5px] font-medium leading-snug text-fg">
                  {toast.title}
                </p>
                {toast.description && (
                  <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted">
                    {toast.description}
                  </p>
                )}
              </div>
              <button
                type="button"
                aria-label="Dismiss"
                onClick={() => dismissToast(toast.id)}
                className="mt-0.5 flex h-5 w-5 flex-none items-center justify-center rounded text-subtle transition-colors hover:bg-hover hover:text-fg"
              >
                <X size={12} />
              </button>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
