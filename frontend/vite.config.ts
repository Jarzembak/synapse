import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const devPublicOrigin =
  process.env.SYNAPSE_DEV_PUBLIC_ORIGIN || "http://localhost:5173";
const devPublicHost = new URL(devPublicOrigin).host;

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        ws: true,
        headers: {
          "X-Synapse-Public-Origin": devPublicOrigin,
          "X-Synapse-Request-Host": devPublicHost,
        },
      },
    },
  },
});
