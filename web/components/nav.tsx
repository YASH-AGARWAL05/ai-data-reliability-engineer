"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export function NavLinks() {
  const evaluation = usePathname().startsWith("/evaluations");
  return (
    <nav aria-label="Primary">
      <Link
        href="/"
        className={`nav-link${evaluation ? "" : " active"}`}
        aria-current={evaluation ? undefined : "page"}
      >
        Workspace
      </Link>
      <Link
        href="/evaluations"
        className={`nav-link${evaluation ? " active" : ""}`}
        aria-current={evaluation ? "page" : undefined}
      >
        Evaluation
      </Link>
    </nav>
  );
}
