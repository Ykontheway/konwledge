import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// A relative base keeps the build portable: the same dist/ works on GitHub Pages
// (served under /konwledge/) and on any static host, because routing uses the hash.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  server: {
    // The snapshot lives in ../demo so that Python and the site share one file.
    fs: { allow: [".."] },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
