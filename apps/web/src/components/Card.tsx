import type { ReactNode } from "react";

export function Card({
  title, children, action,
}: {
  title?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-card)]">
      {(title || action) && (
        <header className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
          {title && <h2 className="text-sm font-medium tracking-wide text-[var(--color-fg-muted)]">{title}</h2>}
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}
