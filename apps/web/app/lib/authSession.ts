const KEY_STORAGE = "apalym_client_access_key";
const LABEL_STORAGE = "apalym_display_label";

export function readStoredAccessKey(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(KEY_STORAGE);
}

export function readStoredDisplayLabel(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(LABEL_STORAGE);
}

export function persistClientSession(accessKey: string, displayLabel: string) {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(KEY_STORAGE, accessKey);
  window.sessionStorage.setItem(LABEL_STORAGE, displayLabel);
}

export function clearClientSession() {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(KEY_STORAGE);
  window.sessionStorage.removeItem(LABEL_STORAGE);
}
