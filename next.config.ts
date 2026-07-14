import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static SPA for Tauri / air-gapped packaging — no Node server routes.
  output: "export",
  images: {
    unoptimized: true,
  },
  trailingSlash: true,
};

export default nextConfig;
