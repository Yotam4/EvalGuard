import type { ReactNode } from "react";

type Tone = "pass" | "warn" | "fail" | "muted" | "info";

const toneClass: Record<Tone, string> = {
  pass:  "bg-[color-mix(in_srgb,var(--color-pass)_18%,transparent)] text-[var(--color-pass)] ring-1 ring-inset ring-[color-mix(in_srgb,var(--color-pass)_30%,transparent)]",
  warn:  "bg-[color-mix(in_srgb,var(--color-warn)_18%,transparent)] text-[var(--color-warn)] ring-1 ring-inset ring-[color-mix(in_srgb,var(--color-warn)_30%,transparent)]",
  fail:  "bg-[color-mix(in_srgb,var(--color-fail)_18%,transparent)] text-[var(--color-fail)] ring-1 ring-inset ring-[color-mix(in_srgb,var(--color-fail)_30%,transparent)]",
  muted: "bg-[var(--color-bg-row)] text-[var(--color-fg-muted)] ring-1 ring-inset ring-[var(--color-border)]",
  info:  "bg-[color-mix(in_srgb,var(--color-accent)_18%,transparent)] text-[var(--color-accent)] ring-1 ring-inset ring-[color-mix(in_srgb,var(--color-accent)_30%,transparent)]",
};

export function Badge({
  tone = "muted",
  children,
}: {
  tone?: Tone;
  children: ReactNode;
}) {
  return (
    <span
      className={
        "inline-flex items-center rounded px-2 py-0.5 text-xs font-medium tracking-wide " +
        toneClass[tone]
      }
    >
      {children}
    </span>
  );
}

/**
 * Convenience: map a run / gate status string to a Badge tone.
 * Centralized here so every page agrees on the mapping.
 */
export function statusTone(status: string | null | undefined): Tone {
  switch (status) {
    case "passed":
      return "pass";
    case "warned":
      return "warn";
    case "failed":
    case "row_failed":
    case "gate_failed":
      return "fail";
    case "cost_capped":
      return "warn";
    default:
      return "muted";
  }
}
