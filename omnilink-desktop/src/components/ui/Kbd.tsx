import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Kbd({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <kbd className={cn("kbd", className)}>{children}</kbd>;
}

export const isMac =
  typeof navigator !== "undefined" &&
  /mac|iphone|ipad/i.test(navigator.platform || "");

export const modKey = isMac ? "⌘" : "Ctrl";
