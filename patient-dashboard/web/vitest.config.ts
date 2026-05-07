import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    globals: true,
    // Default to node so the FhirClient tests in src/lib/fhir/__tests__
    // continue to run as they did before. Component tests opt into jsdom
    // via a per-file `// @vitest-environment jsdom` directive.
    environment: "node",
    include: [
      "src/**/__tests__/**/*.test.{ts,tsx}",
      "src/**/*.test.{ts,tsx}",
    ],
    setupFiles: ["./vitest.setup.ts"],
  },
});
