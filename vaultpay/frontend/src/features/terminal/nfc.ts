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
      else onError("이 태그는 올바른 NFC UID를 알려주지 않아요. 다른 카드를 대 주세요.");
    }, { signal });
    reader.addEventListener("readingerror", () => {
      onError("Chrome이 태그를 읽지 못했어요. NDEF 호환 카드를 다시 대 주세요.");
    }, { signal });
    await reader.scan({ signal });
  } catch (error) {
    if (signal.aborted) return;
    const name = error instanceof DOMException || error instanceof Error ? error.name : "";
    if (name === "NotAllowedError" || name === "SecurityError") {
      throw new Error("NFC 권한이 거부됐어요. Chrome 사이트 설정에서 NFC를 허용한 뒤 다시 시도해 주세요.");
    }
    if (name === "NotSupportedError" || name === "NotReadableError") {
      throw new Error("NFC를 쓸 수 없어요. 휴대폰 설정에서 NFC가 켜져 있는지 확인해 주세요.");
    }
    throw new Error("NFC 스캔이 중단됐어요. 스캔을 다시 시작해 주세요.");
  }
}
