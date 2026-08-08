export function cls(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

/** Open a URL in the system browser (Electron) or a new tab (plain vite). */
export function openExternal(url: string): void {
  if (window.scout) {
    void window.scout.app.openExternal(url);
  } else {
    window.open(url, '_blank', 'noopener');
  }
}
