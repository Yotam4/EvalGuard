"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const items = [
  { href: "/runs",     label: "Runs" },
  { href: "/calls",    label: "Calls" },
  { href: "/config",   label: "Config" },   // PROXY-4 — config editor
  { href: "/reviews",  label: "Reviews" },
  { href: "/golden",   label: "Golden" },
  { href: "/assets",   label: "Assets" },
  { href: "/projects", label: "Projects" },
  { href: "/keys",     label: "Keys" },
  { href: "/orgs",     label: "Orgs" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="border-b border-[var(--color-border)] bg-[var(--color-bg-card)]">
      <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
        <Link href="/runs" className="font-semibold tracking-tight text-[var(--color-accent)]">
          EvalGuard
        </Link>
        <div className="flex gap-1 text-sm">
          {items.map((it) => {
            const active = pathname?.startsWith(it.href);
            return (
              <Link
                key={it.href}
                href={it.href}
                className={
                  "rounded px-3 py-1.5 transition " +
                  (active
                    ? "bg-[var(--color-bg-row)] text-[var(--color-fg)]"
                    : "text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)] hover:text-[var(--color-fg)]")
                }
              >
                {it.label}
              </Link>
            );
          })}
        </div>
      </div>
    </nav>
  );
}
