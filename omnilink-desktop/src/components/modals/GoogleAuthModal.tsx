import { ExternalLink, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Dialog } from "@/components/ui/Dialog";
import { useApp } from "@/store/app";
import { oauthPoll, oauthClear, setSecret } from "@/lib/tauri";

export function GoogleAuthModal({
  open,
  url,
}: {
  open: boolean;
  url: string;
}) {
  const closeModal = useApp((state) => state.closeModal);
  const pushToast = useApp((state) => state.pushToast);
  const [status, setStatus] = useState<"waiting" | "success" | "timeout">("waiting");

  useEffect(() => {
    if (!open || !url) return;
    setStatus("waiting");
    let cancelled = false;
    // Prevent any navigation
    const preventNav = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', preventNav);
    const poll = async () => {
      // Extract port from URL or use default
      const port = 8765;
      for (let i = 0; i < 180; i++) {
        if (cancelled) break;
        await new Promise((r) => setTimeout(r, 1000));
        try {
          const token = await oauthPoll(port);
          if (token && !cancelled) {
            await setSecret("account.sessionToken", token);
            await oauthClear(port);
            setStatus("success");
            pushToast({
              title: "Signed in",
              description: "Google account connected.",
              tone: "success",
            });
            setTimeout(() => closeModal(), 1000);
            return;
          }
        } catch {}
      }
      if (!cancelled) {
        setStatus("timeout");
      }
    };
    void poll();
    return () => {
      cancelled = true;
      window.removeEventListener('beforeunload', preventNav);
    };
  }, [open, url, closeModal, pushToast]);

  return (
    <Dialog open={open} onClose={closeModal} title="Sign in with Google">
      <div className="space-y-4 p-5">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-gradient-to-br from-blue-500 to-red-500 text-white">
            G
          </div>
          <div>
            <p className="text-[13px] font-medium text-fg">Google Sign In</p>
            <p className="text-[10.5px] text-subtle">Complete sign-in in your browser</p>
          </div>
        </div>

        {status === "waiting" && (
          <div className="flex items-center gap-2 rounded-lg border border-line bg-[var(--bg-base)] p-3">
            <Loader2 size={14} className="animate-spin text-accent-text" />
            <div className="flex-1">
              <p className="text-[11px] text-fg">Waiting for sign-in...</p>
              <p className="text-[9.5px] text-subtle">A browser window should have opened</p>
            </div>
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="btn h-7 text-[10px]"
            >
              <ExternalLink size={11} />
              Open again
            </a>
          </div>
        )}

        {status === "success" && (
          <div className="flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--success)_30%,transparent)] bg-[color-mix(in_srgb,var(--success)_10%,transparent)] p-3">
            <div className="flex h-5 w-5 items-center justify-center rounded-full bg-success text-white">
              ✓
            </div>
            <p className="text-[11px] text-success">Successfully signed in!</p>
          </div>
        )}

        {status === "timeout" && (
          <div className="flex items-center gap-2 rounded-lg border border-line bg-[var(--bg-base)] p-3">
            <p className="text-[11px] text-subtle">Sign-in timed out. Please try again.</p>
          </div>
        )}
      </div>
    </Dialog>
  );
}
