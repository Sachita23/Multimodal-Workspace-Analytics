import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Must match the Flask API port (WORKSPACE_API_PORT, default 5000).
const apiPort = process.env.WORKSPACE_API_PORT || "5000";

export default defineConfig({
  plugins: [react()],

  server: {
    port: 5173,

    proxy: {
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: true,
        secure: false,
      },
    },
  },
});
