import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Sidebar } from "@/components/Sidebar";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "DocuVision Control Center",
  description:
    "Human-in-the-Loop verification station for DocuVision document pipeline",
  metadataBase: new URL("https://docuvision.preebg.dev"),
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark h-full">
      <body
        className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      >
        <div className="flex h-full min-h-screen bg-background">
          <Sidebar />
          <main className="min-w-0 flex-1 overflow-hidden">{children}</main>
        </div>
      </body>
    </html>
  );
}
