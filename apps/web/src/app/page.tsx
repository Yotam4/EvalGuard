import { redirect } from "next/navigation";

// Root path → /runs. Static export prerenders this to a tiny
// HTML file that issues a redirect — no Node runtime required.
export default function Index() {
  redirect("/runs");
}
