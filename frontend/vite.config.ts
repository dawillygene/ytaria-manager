/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

/** Adds a strict CSP to production builds (the Nginx deployment also sends one as a header). */
function csp(apiBase: string): Plugin {
  return {
    name: "ytaria-csp",
    apply: "build",
    transformIndexHtml(html) {
      const connect = ["'self'", apiBase].filter(Boolean).join(" ");
      const policy = [
        "default-src 'self'",
        `connect-src ${connect}`,
        "img-src 'self' data:",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",
        "script-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
      ].join("; ");
      return html.replace("<head>", `<head>\n    <meta http-equiv="Content-Security-Policy" content="${policy}" />`);
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiBase = env.VITE_API_BASE_URL ?? "";
  if (mode === "androidrelease" && !/^https:\/\/[^/]+$/.test(apiBase)) {
    // Release builds must bundle assets and talk to a real HTTPS origin; never a dev/cleartext URL.
    throw new Error("androidrelease builds require VITE_API_BASE_URL=https://your-domain (set it in .env.androidrelease.local)");
  }
  return {
    plugins: [react(), tailwindcss(), csp(apiBase)],
    server: {
      port: 5173,
      // Same-origin in development so cookie auth behaves exactly like production.
      proxy: { "/api": { target: env.VITE_DEV_PROXY_TARGET || "http://127.0.0.1:8000", changeOrigin: false } },
    },
    build: { sourcemap: false, target: "es2022" },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      globals: true,
      css: false,
    },
  };
});
