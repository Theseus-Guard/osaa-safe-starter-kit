import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    // In development the gateway from docker compose serves /api, same as in production.
    // The gateway serves HTTPS with a self-signed development CA, so the certificate is not verified here.
    proxy: { "/api": { target: "https://127.0.0.1:8443", secure: false } },
  },
});
