import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";

import { Kbd } from "@/components/ui/Kbd";

interface Step {
  id: string;
  title: string;
  body: string;
  target?: string; // CSS selector
  placement?: "top" | "bottom" | "left" | "right" | "center";
}

const STEPS: Step[] = [
  {
    id: "welcome",
    title: "Welcome to OmniLink",
    body: "A local-first workspace for chat, coding, MCP tools, and prompts. Let's take a 60-second tour — or skip and explore on your own.",
    placement: "center",
  },
  {
    id: "rail",
    title: "Navigate with the rail",
    body: "The left rail switches between Chat, Coding, MCP, Prompts, and Settings. Tooltips show shortcuts.",
    target: 'nav[aria-label="Primary navigation"]',
    placement: "right",
  },
  {
    id: "chat",
    title: "Chat",
    body: "Start a conversation, draft messages, and stream replies from your connected model.",
    target: 'button[aria-label="Chat"]',
    placement: "right",
  },
  {
    id: "composer",
    title: "Composer",
    body: "Type a prompt and press Enter. Shift+Enter adds a new line. Attach context from the coding view.",
    target: "textarea[data-composer]",
    placement: "top",
  },
  {
    id: "coding",
    title: "Coding workspace",
    body: "Create projects, edit files in Monaco, and preview HTML. The agent panel proposes edits with diffs.",
    target: 'button[aria-label="Coding"]',
    placement: "right",
  },
  {
    id: "mcp",
    title: "MCP servers",
    body: "Register stdio, SSE, and HTTP MCP servers. Test connections to discover tools.",
    target: 'button[aria-label="MCP"]',
    placement: "right",
  },
  {
    id: "prompts",
    title: "Prompt library",
    body: "Save reusable instructions and inject them into any chat with one click.",
    target: 'button[aria-label="Prompts"]',
    placement: "right",
  },
  {
    id: "palette",
    title: "Command palette",
    body: "Press Ctrl+K to jump anywhere, create projects, and change appearance.",
    target: 'button[aria-label="Search or jump to…"]',
    placement: "bottom",
  },
  {
    id: "settings",
    title: "Settings",
    body: "Themes, accents, models, storage, and account. Everything stays on this device by default.",
    target: 'button[aria-label="Settings"]',
    placement: "right",
  },
  {
    id: "done",
    title: "You're ready",
    body: "Replay this tour anytime from Settings → About. Press Ctrl+K to get started.",
    placement: "center",
  },
];

export function TutorialOverlay() {
  const [visible, setVisible] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);

  useEffect(() => {
    const done = window.localStorage.getItem("omnilink.tutorialDone");
    if (!done) {
      const timer = window.setTimeout(() => setVisible(true), 900);
      return () => window.clearTimeout(timer);
    }
  }, []);

  const step = STEPS[stepIndex];

  useEffect(() => {
    if (!visible) return;
    const update = () => {
      if (step.target) {
        const el = document.querySelector(step.target);
        if (el) {
          setRect(el.getBoundingClientRect());
        } else {
          setRect(null);
        }
      } else {
        setRect(null);
      }
    };
    update();
    const onResize = () => update();
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onResize, true);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onResize, true);
    };
  }, [visible, stepIndex, step.target]);

  const finish = () => {
    window.localStorage.setItem("omnilink.tutorialDone", "1");
    setVisible(false);
  };

  const next = () => {
    if (stepIndex >= STEPS.length - 1) {
      finish();
    } else {
      setStepIndex((i) => i + 1);
    }
  };

  const back = () => {
    if (stepIndex > 0) setStepIndex((i) => i - 1);
  };

  if (!visible) return null;

  const padding = 8;
  const spotlightStyle = rect
    ? {
        top: rect.top - padding,
        left: rect.left - padding,
        width: rect.width + padding * 2,
        height: rect.height + padding * 2,
      }
    : null;

  // Tooltip position
  const cardW = 340;
  const cardH = 220;
  let tooltipStyle: React.CSSProperties = {
    top: "50%",
    left: "50%",
    transform: "translate(-50%, -50%)",
  };
  if (rect && step.placement !== "center") {
    const gap = 16;
    if (step.placement === "right") {
      let top = rect.top + rect.height / 2 - cardH / 2;
      top = Math.max(12, Math.min(window.innerHeight - cardH - 12, top));
      let left = rect.right + gap;
      if (left + cardW > window.innerWidth - 12) left = window.innerWidth - cardW - 12;
      tooltipStyle = { top, left };
    } else if (step.placement === "left") {
      let top = rect.top + rect.height / 2 - cardH / 2;
      top = Math.max(12, Math.min(window.innerHeight - cardH - 12, top));
      let right = window.innerWidth - rect.left + gap;
      if (right + cardW > window.innerWidth - 12) right = window.innerWidth - cardW - 12;
      tooltipStyle = { top, right };
    } else if (step.placement === "bottom") {
      let left = rect.left + rect.width / 2 - cardW / 2;
      left = Math.max(12, Math.min(window.innerWidth - cardW - 12, left));
      tooltipStyle = { top: rect.bottom + gap, left };
    } else if (step.placement === "top") {
      let left = rect.left + rect.width / 2 - cardW / 2;
      left = Math.max(12, Math.min(window.innerWidth - cardW - 12, left));
      tooltipStyle = { bottom: window.innerHeight - rect.top + gap, left };
    }
  }

  return (
    <AnimatePresence>
      <motion.div
        key="tutorial-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.3 }}
        className="fixed inset-0 z-[100]"
      >
        {/* Dimmed backdrop with spotlight hole */}
        {spotlightStyle ? (
          <>
            <motion.div
              className="fixed bg-black/65"
              style={{ top: 0, left: 0, right: 0, height: spotlightStyle.top }}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
            />
            <motion.div
              className="fixed bg-black/65"
              style={{
                top: spotlightStyle.top + spotlightStyle.height,
                left: 0,
                right: 0,
                bottom: 0,
              }}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
            />
            <motion.div
              className="fixed bg-black/65"
              style={{
                top: spotlightStyle.top,
                left: 0,
                width: spotlightStyle.left,
                height: spotlightStyle.height,
              }}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
            />
            <motion.div
              className="fixed bg-black/65"
              style={{
                top: spotlightStyle.top,
                left: spotlightStyle.left + spotlightStyle.width,
                right: 0,
                height: spotlightStyle.height,
              }}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
            />
            <motion.div
              layoutId="spotlight"
              className="fixed rounded-xl ring-2 ring-accent shadow-[0_0_0_9999px_rgba(0,0,0,0)]"
              style={spotlightStyle}
              transition={{ type: "spring", stiffness: 300, damping: 30 }}
            >
              <div className="absolute inset-0 rounded-xl ring-2 ring-accent/70 shadow-[0_0_30px_var(--accent-soft)]" />
            </motion.div>
          </>
        ) : (
          <motion.div
            className="absolute inset-0 bg-black/65"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
          />
        )}

        {/* Coach mark card */}
        <motion.div
          key={step.id}
          initial={{ opacity: 0, y: 20, scale: 0.94 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -12, scale: 0.94 }}
          transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
          className={`fixed z-[101] rounded-2xl border border-line bg-[var(--bg-elevated)] p-6 shadow-2xl ${
            step.placement === "center"
              ? "w-[460px] max-w-[92vw]"
              : "w-[340px] max-w-[90vw]"
          }`}
          style={tooltipStyle}
        >
          <div className="mb-1 flex items-center gap-2">
            <span className="text-[9.5px] font-semibold uppercase tracking-[0.16em] text-accent-text">
              Step {stepIndex + 1} of {STEPS.length}
            </span>
          </div>
          {step.id === "welcome" && (
            <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-line bg-[var(--bg-base)] text-accent-text shadow-lg">
              <span className="text-[24px] font-bold">∞</span>
            </div>
          )}
          <h3 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
            {step.title}
          </h3>
          <p className="mt-2 text-[12px] leading-relaxed text-muted">
            {step.body}
          </p>
          {step.id === "palette" && (
            <div className="mt-3 flex items-center gap-1.5">
              <Kbd>Ctrl</Kbd>
              <Kbd>K</Kbd>
            </div>
          )}
          <div className="mt-4 flex items-center justify-between">
            <button
              type="button"
              onClick={finish}
              className="text-[10.5px] text-subtle transition-colors hover:text-fg"
            >
              Skip tour
            </button>
            <div className="flex items-center gap-2">
              {stepIndex > 0 && (
                <button
                  type="button"
                  onClick={back}
                  className="btn btn-ghost h-7 text-[10.5px]"
                >
                  Back
                </button>
              )}
              <button
                type="button"
                onClick={next}
                className="btn btn-primary h-7 text-[10.5px]"
              >
                {stepIndex >= STEPS.length - 1 ? "Finish" : "Next"}
              </button>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
