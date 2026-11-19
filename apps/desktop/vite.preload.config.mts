import { defineConfig } from 'vite';

// Sandboxed preload must be a single CommonJS file.
export default defineConfig({
  build: {
    rollupOptions: {
      output: { entryFileNames: 'preload.js' },
    },
  },
});
