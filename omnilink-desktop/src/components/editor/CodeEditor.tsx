import { lazy, Suspense } from "react";

import type { CodeEditorProps } from "@/components/editor/MonacoEditor";

const MonacoEditor = lazy(() => import("@/components/editor/MonacoEditor"));

function EditorSkeleton() {
  return (
    <div className="relative flex h-full flex-col gap-2 overflow-hidden bg-[var(--bg-surface)] px-5 py-5">
      <div className="mb-2 h-3 w-40 rounded bg-[var(--bg-active)]" />
      {[62, 48, 72, 40, 56, 34, 64, 44, 52].map((width, index) => (
        <div
          key={index}
          className="shimmer h-2.5 rounded"
          style={{ width: `${width}%` }}
        />
      ))}
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <span className="rounded-full border border-line bg-[var(--bg-elevated)] px-3 py-1 text-[10px] text-subtle shadow-lg">
          Loading editor…
        </span>
      </div>
    </div>
  );
}

export function CodeEditor(props: CodeEditorProps) {
  return (
    <Suspense fallback={<EditorSkeleton />}>
      <MonacoEditor {...props} />
    </Suspense>
  );
}
