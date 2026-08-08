import type { ScoutApi } from '@shared/types';

declare global {
  interface Window {
    /** Present only inside Electron (exposed by the preload). Absent when the
     *  renderer runs under plain `vite` in a browser. */
    scout?: ScoutApi;
  }
}

export {};
