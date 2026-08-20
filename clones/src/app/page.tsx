import Link from "next/link";
import { CLONES } from "@/clones";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-24">
      <h1 className="text-2xl font-semibold tracking-tight">Clone workspace</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Reverse-engineered reference sites. Build one with{" "}
        <code className="font-mono text-foreground">/clone-website &lt;url&gt;</code>.
      </p>

      {CLONES.length === 0 ? (
        <p className="mt-10 text-sm text-muted-foreground">Nothing cloned yet.</p>
      ) : (
        <ul className="mt-10 divide-y divide-border border-y border-border">
          {CLONES.map((clone) => (
            <li key={clone.route} className="py-4">
              <Link
                href={clone.route}
                className="text-sm font-medium underline-offset-4 hover:underline"
              >
                {clone.label}
              </Link>
              <p className="mt-1 font-mono text-xs text-muted-foreground">
                {clone.source} · cloned {clone.clonedAt}
              </p>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
