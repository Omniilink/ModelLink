import { cn } from "@/lib/utils";

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      data-on={checked ? "true" : "false"}
      onClick={() => onChange(!checked)}
      className={cn("switch", disabled && "opacity-40")}
    >
      <span className="switch-thumb" />
    </button>
  );
}
