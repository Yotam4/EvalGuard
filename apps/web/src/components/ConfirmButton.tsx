"use client";

import { useState } from "react";

/**
 * Two-click guard for destructive actions. The first click shows
 * a confirm prompt inline; the second commits.
 *
 * Avoiding ``window.confirm`` deliberately: the native dialog
 * has terrible visual integration with a dark theme, blocks the
 * thread, and isn't accessible from screen readers in some browsers.
 * This component renders inline so the user stays in context.
 */
export function ConfirmButton({
  onConfirm,
  label = "Delete",
  confirmLabel = "Click again to confirm",
  pending = false,
}: {
  onConfirm: () => void;
  label?: string;
  confirmLabel?: string;
  pending?: boolean;
}) {
  const [armed, setArmed] = useState(false);

  if (pending) {
    return (
      <button
        type="button"
        disabled
        className="rounded border border-[var(--color-border)] px-2 py-1 text-xs opacity-50"
      >
        …
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={() => {
        if (armed) {
          onConfirm();
          setArmed(false);
        } else {
          setArmed(true);
          // Auto-disarm after 4s so a stray click doesn't sit hot.
          setTimeout(() => setArmed(false), 4000);
        }
      }}
      onBlur={() => setArmed(false)}
      className={
        "rounded border px-2 py-1 text-xs transition " +
        (armed
          ? "border-[var(--color-fail)] bg-[color-mix(in_srgb,var(--color-fail)_18%,transparent)] text-[var(--color-fail)]"
          : "border-[var(--color-border)] text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)]")
      }
    >
      {armed ? confirmLabel : label}
    </button>
  );
}
