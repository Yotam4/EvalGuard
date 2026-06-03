/**
 * Reusable tab/radiogroup strip with keyboard navigation.
 *
 * Two ARIA flavours via the ``variant`` prop:
 *
 * - ``variant="tabs"`` — proper W3C tabs pattern (``role=tablist`` +
 *   ``role=tab`` + ``aria-selected``).  Use only when the tabs reveal
 *   a ``tabpanel``.
 *
 * - ``variant="radiogroup"`` (default) — single-select filter
 *   semantics (``role=radiogroup`` + ``role=radio`` + ``aria-checked``).
 *   Round-8 review-pass: the previous tablist-only API was wrong for
 *   filter usage on /runs and /assets — screen readers announced
 *   "tab, 1 of N" and looked for an ``aria-controls``'d region that
 *   doesn't exist.  /calls had already switched to a hand-rolled
 *   radiogroup; this consolidates the pattern.
 *
 * Both variants share roving tabindex + ArrowLeft/Right + Home/End
 * keyboard support.
 *
 * Generic over the option value type so it works for the source
 * filter (``null | "cli" | "otlp"``) and the asset kind filter
 * (``AssetKind``) without runtime casts.
 *
 * Reference: https://www.w3.org/WAI/ARIA/apg/patterns/tabs/
 *            https://www.w3.org/WAI/ARIA/apg/patterns/radio/
 */

"use client";

import { useRef } from "react";


export interface TabSpec<T> {
  value: T;
  label: string;
  /** Optional ``data-*`` attribute value for the rendered button —
   *  lets parents address specific tabs from Playwright / vitest
   *  without depending on the visible label. */
  dataAttr?: string;
}


export function Tabs<T>({
  value, onChange, tabs,
  ariaLabel,
  testid,
  dataAttrName,
  variant = "radiogroup",
}: {
  value: T;
  onChange: (v: T) => void;
  tabs: TabSpec<T>[];
  /** Required for screen readers — names the group (e.g.
   *  "Filter runs by source"). */
  ariaLabel: string;
  /** Optional ``data-testid`` on the container. */
  testid?: string;
  /** Name of the per-button ``data-*`` attribute (e.g. ``"source-tab"``
   *  → ``data-source-tab="otlp"``).  When omitted the attribute is
   *  skipped. */
  dataAttrName?: string;
  /** ``radiogroup`` (default) for single-select filters,
   *  ``tabs`` when the component reveals a ``tabpanel``. */
  variant?: "tabs" | "radiogroup";
}) {
  const isTabs   = variant === "tabs";
  const groupRole = isTabs ? "tablist" : "radiogroup";
  const itemRole  = isTabs ? "tab"     : "radio";
  // One ref per tab so the keyboard handler can ``focus()`` the
  // sibling that should receive focus on Arrow.  Using a ref array
  // (not a callback ref map) because the tab count is stable for
  // the lifetime of a tablist.
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const activeIndex = Math.max(
    0,
    tabs.findIndex((t) => Object.is(t.value, value)),
  );

  function focusTab(i: number) {
    const len = tabs.length;
    // Wrap-around: ArrowLeft on the first tab goes to the last.
    const next = ((i % len) + len) % len;
    const btn  = buttonRefs.current[next];
    if (btn) {
      btn.focus();
      onChange(tabs[next].value);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    switch (e.key) {
      case "ArrowRight":
        e.preventDefault();
        focusTab(activeIndex + 1);
        break;
      case "ArrowLeft":
        e.preventDefault();
        focusTab(activeIndex - 1);
        break;
      case "Home":
        e.preventDefault();
        focusTab(0);
        break;
      case "End":
        e.preventDefault();
        focusTab(tabs.length - 1);
        break;
    }
  }

  return (
    <div
      role={groupRole}
      aria-label={ariaLabel}
      data-testid={testid}
      onKeyDown={onKeyDown}
      className="flex flex-wrap gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] p-1"
    >
      {tabs.map((t, i) => {
        const active = i === activeIndex;
        const extra: Record<string, string | boolean> = {};
        if (dataAttrName && t.dataAttr !== undefined) {
          extra[`data-${dataAttrName}`] = t.dataAttr;
        }
        // ``aria-selected`` for tabs, ``aria-checked`` for radios.
        // Setting BOTH would be technically valid but redundant; SRs
        // pick the matching one for the parent role.
        if (isTabs) extra["aria-selected"] = active;
        else        extra["aria-checked"]  = active;
        return (
          <button
            key={t.label}
            ref={(el) => { buttonRefs.current[i] = el; }}
            type="button"
            role={itemRole}
            // Roving tabindex — only the active item is in the
            // page's tab order, the rest are reachable via Arrow.
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(t.value)}
            {...extra}
            className={
              "rounded px-3 py-1.5 text-sm transition focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)] " +
              (active
                ? "bg-[var(--color-bg-row)] text-[var(--color-fg)]"
                : "text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)] hover:text-[var(--color-fg)]")
            }
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
