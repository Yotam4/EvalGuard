/**
 * "Promote to golden" button — sits on the call detail panel.
 *
 * Lives in its own file so the mutation lifecycle (idle → pending
 * → success-chip) is unit-testable in isolation.  Server UPSERT
 * means re-clicking is idempotent — no need to dedupe client-side.
 */

"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { promoteToGolden, unPromoteGolden } from "@/lib/api";


/**
 * NOTE for consumers: the mutation state (idle / pending / success)
 * sticks for the lifetime of this component instance.  If a parent
 * renders one ``<PromoteButton>`` and swaps its ``runId``/``rowId``
 * via prop updates (rather than unmount + remount), the previously-
 * promoted row's "✓ Promoted" label will carry over onto the new
 * row.  The fix is consumer-side: pass ``key={`${runId}:${rowId}`}``
 * so React unmounts + remounts on row change.  This is the
 * idiomatic React pattern for "identity changes when props change"
 * and avoids the timing-fragile ``useEffect(reset, [runId, rowId])``
 * dance.  ``CallDetailPanel`` does exactly that.
 */
export function PromoteButton({
  runId,
  rowId,
  projectSlug,
}: {
  runId: string;
  rowId: string;
  /** When provided, the project's golden-list query is invalidated
   *  on success so a sibling ``<GoldenList>`` refetches. */
  projectSlug?: string;
}) {
  const qc = useQueryClient();
  const [note, setNote] = useState<string>("");
  const [editing, setEditing] = useState<boolean>(false);

  const m = useMutation({
    mutationFn: () => promoteToGolden({
      run_id: runId, row_id: rowId,
      note: note.trim() || undefined,
    }),
    onSuccess: () => {
      setEditing(false);
      setNote("");
      if (projectSlug) {
        qc.invalidateQueries({
          queryKey: ["golden-candidates", projectSlug],
        });
      }
    },
  });

  // Round-8 review-pass: surface an inline Undo affordance after a
  // successful promote.  Previously the only feedback was the
  // button-label flip to "✓ Promoted" and a mis-click forced the
  // operator to navigate to /golden, find the candidate, and
  // un-promote there.  ``m.data.id`` is the row id returned by the
  // POST and is the same id ``unPromoteGolden`` consumes.
  const undo = useMutation({
    mutationFn: (id: number) => unPromoteGolden(id),
    onSuccess: () => {
      m.reset();
      if (projectSlug) {
        qc.invalidateQueries({
          queryKey: ["golden-candidates", projectSlug],
        });
      }
    },
  });

  if (!editing) {
    if (m.isSuccess && m.data) {
      return (
        <span className="inline-flex items-center gap-1.5 text-xs">
          <span
            data-testid="promote-success"
            className="text-[var(--color-pass)]"
          >
            ✓ Promoted
          </span>
          <button
            type="button"
            data-testid="promote-undo"
            disabled={undo.isPending}
            onClick={() => undo.mutate(m.data.id)}
            className="rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)] hover:text-[var(--color-fg)] disabled:opacity-40"
          >
            {undo.isPending ? "undoing…" : "undo"}
          </button>
        </span>
      );
    }
    return (
      <button
        type="button"
        data-testid="promote-button"
        onClick={() => setEditing(true)}
        className="rounded border border-[var(--color-border)] px-2 py-0.5 text-xs hover:bg-[var(--color-bg-row)]"
      >
        Promote to golden
      </button>
    );
  }

  return (
    <form
      data-testid="promote-form"
      onSubmit={(e) => { e.preventDefault(); m.mutate(); }}
      className="flex flex-wrap items-center gap-2"
    >
      <input
        type="text"
        placeholder="Optional note"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        maxLength={4000}
        className="flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 text-xs"
      />
      <button
        type="submit"
        disabled={m.isPending}
        data-testid="promote-confirm"
        className={
          "rounded border px-2 py-0.5 text-xs " +
          (m.isPending
            ? "cursor-not-allowed border-[var(--color-border)] text-[var(--color-fg-muted)]"
            : "border-[var(--color-accent)] hover:bg-[var(--color-bg-row)]")
        }
      >
        {m.isPending ? "Saving…" : "Save"}
      </button>
      <button
        type="button"
        onClick={() => { setEditing(false); setNote(""); }}
        className="rounded border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)]"
      >
        cancel
      </button>
      {m.error && (
        <span
          data-testid="promote-error"
          className="text-xs text-[var(--color-fail)]"
        >
          {m.error instanceof Error ? m.error.message : String(m.error)}
        </span>
      )}
    </form>
  );
}
