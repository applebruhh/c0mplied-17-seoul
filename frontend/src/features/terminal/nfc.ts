type NFCReader = EventTarget & { scan(options: { signal: AbortSignal }): Promise<void> };
type NFCReadingEvent = Event & { serialNumber?: string };

export function normalizeUid(serialNumber?: string): string | null {
  const uid = serialNumber?.replace(/[:\s-]/g, "");
  return uid && uid.length <= 100 && /^(?:[0-9a-f]{2})+$/i.test(uid) ? uid.toUpperCase() : null;
}

export function getNfcReader() {
  if (!window.isSecureContext || window.top !== window.self) return undefined;
  return (window as Window & { NDEFReader?: new () => NFCReader }).NDEFReader;
}

export async function scanUid(
  Reader: new () => NFCReader,
  signal: AbortSignal,
  onUid: (uid: string) => void,
  onError: (message: string) => void,
) {
  try {
    const reader = new Reader();
    reader.addEventListener("reading", (event) => {
      if (signal.aborted) return;
      const uid = normalizeUid((event as NFCReadingEvent).serialNumber);
      if (uid) onUid(uid);
      else onError("This tag did not report a valid NFC UID. Try another tag.");
    }, { signal });
    reader.addEventListener("readingerror", () => {
      onError("Chrome could not read the tag. Try an NDEF-compatible tag.");
    }, { signal });
    await reader.scan({ signal });
  } catch (error) {
    if (signal.aborted) return;
    const name = error instanceof DOMException || error instanceof Error ? error.name : "";
    if (name === "NotAllowedError" || name === "SecurityError") {
      throw new Error("NFC access was denied. Allow NFC in Chrome site settings and try again.");
    }
    if (name === "NotSupportedError" || name === "NotReadableError") {
      throw new Error("NFC is unavailable. Check that your phone supports NFC and that NFC is turned on.");
    }
    throw new Error("NFC scanning was interrupted. Start a new scan to try again.");
  }
}
