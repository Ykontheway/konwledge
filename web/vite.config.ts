import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import { managementProxy } from "./dev/management-proxy.ts";

// A relative base keeps the build portable: the same dist/ works on GitHub Pages
// (served under /konwledge/) and on any static host, because routing uses the hash.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss(), managementProxy()],
  envPrefix: "VITE_",
  server: {
    host: "127.0.0.1",
    allowedHosts: ["localhost"],
    cors: false,
    strictPort: true,
    // The snapshot lives in ../demo so that Python and the site share one file.
    fs: { allow: [".."] },
  },
  preview: { host: "127.0.0.1", allowedHosts: ["localhost"], cors: false, proxy: {} },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx", "dev/**/*.test.ts"],
  },
});
