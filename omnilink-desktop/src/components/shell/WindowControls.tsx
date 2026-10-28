import { useEffect, useState } from "react";
import { Minus, Square, X } from "lucide-react";

import { windowControls } from "@/lib/tauri";

function RestoreIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden>
      <rect
        x="1.25"
        y="3.25"
        width="6.5"
        height="6.5"
        rx="1"
        stroke="currentColor"
        strokeWidth="1.1"
      />
      <path
        d="M3.5 3V2.25c0-.55.45-1 1-1h4.25c.55 0 1 .45 1 1V6.5c0 .55-.45 1-1 1H8"
        stroke="currentColor"
        strokeWidth="1.1"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function WindowControls() {
  const controls = windowControls();
  const [maximized, setMaximized] = useState(false);

  useEffect(() => {
    if (!controls) return;
    let mounted = true;
    const update = () => {
      controls
        .isMaximized()
        .then((value) => {
          if (mounted) setMaximized(value);
        })
        .catch(() => undefined);
    };
    update();
    let unlisten: (() => void) | undefined;
    controls
      .onResized(update)
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => undefined);
    return () => {
      mounted = false;
      unlisten?.();
    };
    // Window handle is stable for the lifetime of the app.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!controls) {
    return <div className="h-full w-[132px]" aria-hidden />;
  }

  return (
    <div className="flex h-full items-stretch">
      <button
        type="button"
        aria-label="Minimize"
        className="titlebar-button"
        onClick={() => controls.minimize()}
      >
        <Minus size={14} strokeWidth={1.6} />
      </button>
      <button
        type="button"
        aria-label={maximized ? "Restore" : "Maximize"}
        className="titlebar-button"
        onClick={() => controls.toggleMaximize()}
      >
        {maximized ? <RestoreIcon /> : <Square size={10} strokeWidth={1.5} />}
      </button>
      <button
        type="button"
        aria-label="Close"
        className="titlebar-button close"
        onClick={() => controls.close()}
      >
        <X size={14} strokeWidth={1.6} />
      </button>
    </div>
  );
}
