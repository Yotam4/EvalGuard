/**
 * Reusable tab strip with proper ARIA semantics + keyboard navigation.
 *
 * The previous ad-hoc tab strips in ``/runs/`` and ``/assets/`` used
 * ``aria-pressed`` (the toggle-button pattern) and had no keyboard
 * support, which is wrong for what behaves like a single-select
 * filter.  This component implements the ARIA Authoring Practices
 * tabs pattern:
 *
 *   - ``role="tablist"`` on the container, ``role="tab"`` on each button
 *   - ``aria-selected`` (not ``aria-pressed``)
 *   - Roving tabindex (active tab is ``tabIndex=0``, others ``-1``)
 *   - ArrowLeft / ArrowRight cycle focus + selection
 *   - Home / End jump to first / last
 *
 * Generic over the option value type so it works for both the
 * source filter (``null | "cli" | "otlp"``) and the asset kind
 * filter (``AssetKind``) without runtime casts.
 *
 * Reference: https://www.w3.org/WAI/ARIA/apg/patterns/tabs/
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
}: {
  value: T;
  onChange: (v: T) => void;
  tabs: TabSpec<T>[];
  /** Required for screen readers — names the group of tabs (e.g.
   *  "Filter runs by source"). */
  ariaLabel: string;
  /** Optional ``data-testid`` on the tablist root. */
  testid?: string;
  /** Name of the per-button ``data-*`` attribute (e.g. ``"source-tab"``
   *  → ``data-source-tab="otlp"``).  When omitted the attribute is
   *  skipped. */
  dataAttrName?: string;
}) {
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
      role="tablist"
      aria-label={ariaLabel}
      data-testid={testid}
      onKeyDown={onKeyDown}
      className="flex flex-wrap gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] p-1"
    >
      {tabs.map((t, i) => {
        const active = i === activeIndex;
        const extra: Record<string, string> = {};
        if (dataAttrName && t.dataAttr !== undefined) {
          extra[`data-${dataAttrName}`] = t.dataAttr;
        }
        return (
          <button
            key={t.label}
            ref={(el) => { buttonRefs.current[i] = el; }}
            type="button"
            role="tab"
            aria-selected={active}
            // Roving tabindex — only the active tab is in the
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
