import type { Metadata } from "next";

export const metadata: Metadata = { title: "Evaluation" };

export default function EvaluationLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return children;
}
