import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "AccountingOS — US Close Readiness",
  description: "Controlled US production close workflow",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
