import type { Metadata } from "next";
import Link from "next/link";
import { NavLinks } from "../components/nav";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Workspace | Signal Desk", template: "%s | Signal Desk" },
  description: "Local data reliability investigations and patch review.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div className="app-shell">
          <aside className="rail">
            <Link className="brand" href="/" aria-label="Signal Desk workspace">
              <span className="brand-mark" aria-hidden="true">
                S
              </span>
              <span>
                signal<span className="brand-soft">desk</span>
              </span>
            </Link>
            <div className="rail-label">OPERATIONS</div>
            <NavLinks />
            <div className="rail-foot">
              <span className="connection-dot" /> Local workbench
            </div>
          </aside>
          <div className="main-area">
            <header className="topbar">
              <span>Data reliability / Investigation workbench</span>
              <span className="topbar-right">LOCAL ENVIRONMENT</span>
            </header>
            <main id="main-content">{children}</main>
          </div>
        </div>
      </body>
    </html>
  );
}
