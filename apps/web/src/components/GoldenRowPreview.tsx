/**
 * Inline preview of a golden candidate's row content (input /
 * expected / output).  Presentational + pure — the parent page
 * fetches ``?expand=row`` and passes ``row_data`` in.
 *
 * Lives in its own file so the value-formatting + truncation logic
 * is unit-testable without the page's react-query / router
 * scaffolding (same convention as DriftBody / AssetVersionsTable).
 */

"use client";

import type { GoldenRowData } from "@/lib/api";


export function GoldenRowPreview({
  rowData,
}: {
  rowData: GoldenRowData | null | undefined;
}) {
  if (!rowData) {
    return (
      <p
        data-testid="golden-preview-unavailable"
        className="text-xs italic text-[var(--color-fg-muted)]"
      >
        row content unavailable (parent run removed)
      </p>
    );
  }
  return (
    <div data-testid="golden-preview" className="space-y-2">
      <Field label="input"    value={rowData.input} />
      <Field label="expected" value={rowData.expected} />
      <Field label="output"   value={rowData.output} />
    </div>
  );
}


function Field({ label, value }: { label: string; value: unknown }) {
  // ``undefined`` means the source row never carried this field;
  // ``null`` means it carried an explicit null.  Both render as a
  // muted dash so the operator can tell "present + empty" from
  // "absent" only by hovering the title (kept minimal on purpose).
  const display = formatValue(value);
  return (
    <div className="grid grid-cols-[5rem_1fr] gap-2 text-xs">
      <span className="text-[var(--color-fg-muted)]">{label}</span>
      <span
        className="whitespace-pre-wrap break-words font-mono text-[var(--color-fg)]"
        data-field={label}
      >
        {display}
      </span>
    </div>
  );
}


/** Render any JSON-ish value as a compact preview string.  Objects
 *  and arrays are JSON-stringified (so a RAG ``contexts`` array or a
 *  structured input is still readable); strings pass through;
 *  null/undefined collapse to a dash. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
