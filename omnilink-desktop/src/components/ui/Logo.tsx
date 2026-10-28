import { useId } from "react";

import { cn } from "@/lib/utils";

export function Logo({
  size = 22,
  className,
  glow = false,
}: {
  size?: number;
  className?: string;
  glow?: boolean;
}) {
  const id = useId().replace(/:/g, "");
  return (
    <span
      className={cn("relative inline-flex flex-none", className)}
      style={{ width: size, height: size }}
    >
      {glow && (
        <span
          aria-hidden
          className="absolute inset-[-45%] rounded-full opacity-50 blur-xl"
          style={{ background: "var(--accent)" }}
        />
      )}
      <svg
        width={size}
        height={size}
        viewBox="0 0 64 64"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        className="relative"
        aria-hidden
      >
        <defs>
          <linearGradient
            id={`${id}-bg`}
            x1="8"
            y1="4"
            x2="58"
            y2="60"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="var(--accent)" />
            <stop offset="1" stopColor="var(--accent-hover)" />
          </linearGradient>
          <linearGradient
            id={`${id}-shine`}
            x1="14"
            y1="8"
            x2="50"
            y2="56"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="white" stopOpacity="0.3" />
            <stop offset="0.55" stopColor="white" stopOpacity="0" />
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="16" fill={`url(#${id}-bg)`} />
        <rect
          x="1"
          y="1"
          width="62"
          height="62"
          rx="15"
          stroke="white"
          strokeOpacity="0.16"
        />
        <path
          d="M2 18C2 9.163 9.163 2 18 2h28c8.837 0 16 7.163 16 16v10H2V18Z"
          fill={`url(#${id}-shine)`}
        />
        <circle cx="25" cy="32" r="9" stroke="white" strokeWidth="4" />
        <circle
          cx="39"
          cy="32"
          r="9"
          stroke="white"
          strokeOpacity="0.8"
          strokeWidth="4"
        />
      </svg>
    </span>
  );
}
