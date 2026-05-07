import type { Metadata } from "next";
import "./globals.css";

import { Nav } from "@/components/Nav";
import { QueryProvider } from "@/lib/query";

export const metadata: Metadata = {
  title:       "EvalGuard",
  description: "EvalGuard UI — runs, gates, and audit trails for your evaluation pipeline.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <QueryProvider>
          <Nav />
          <main className="mx-auto max-w-6xl px-6 py-6">{children}</main>
        </QueryProvider>
      </body>
    </html>
  );
}
