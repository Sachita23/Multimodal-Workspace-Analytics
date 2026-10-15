import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Must match the Flask API port (config.API_PORT / WORKSPACE_API_PORT,
// default 5050; macOS AirPlay already holds 5000).
const apiPort = process.env.WORKSPACE_API_PORT || "5050";

export default defineConfig({
  plugins: [react()],

  server: {
    port: 5173,

    // Fail loudly instead of silently moving to 5174, so the URL that
    // start_project.py opens is always the right one.
    strictPort: true,

    proxy: {
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: true,
        secure: false,
      },
    },
  },
});
