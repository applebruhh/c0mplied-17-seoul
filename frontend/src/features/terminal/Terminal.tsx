import { useCallback, useEffect, useRef, useState } from "react";
import type { CheckoutRead } from "../../shared/contracts";
import { getActiveCheckout, getCapabilities, getCheckout, postTap, RequestRejected, type CheckoutTap } from "./api";
import { getNfcReader, scanUid } from "./nfc";

const DEMO_CARD = "demo-alice";
const formatKrw = new Intl.NumberFormat("ko-KR", { style: "currency", currency: "KRW" });
const button = "rounded border border-current px-3 py-2 disabled:opacity-50";

export default function Terminal() {
  const [terminalId] = useState(() => new URLSearchParams(window.location.search).get("terminal_id") || "cafe-01");
  const [checkout, setCheckout] = useState<CheckoutRead | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [demo, setDemo] = useState(false);
  const [scanStatus, setScanStatus] = useState<"STARTING_SCAN" | "WAITING_FOR_CARD" | null>(null);
  const [card, setCard] = useState<{ checkoutId: string; alias: string } | null>(null);
  const [attempt, setAttempt] = useState<{ body: CheckoutTap; unknown: boolean } | null>(null);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const scan = useRef<AbortController | null>(null);
  const sending = useRef<AbortController | null>(null);
  const lastTap = useRef<{ checkoutId: string; rejected: boolean } | null>(null);

  const reader = getNfcReader();
  const trackingId = attempt?.body.checkout_id ?? (checkout?.status === "AUTHORIZING" ? checkout.checkout_id : null);
  const submitting = attempt !== null && !attempt.unknown;
  const readyId = checkout?.status === "WAITING_FOR_CARD" && !loadError && !attempt ? checkout.checkout_id : null;

  const stopScan = useCallback(() => {
    scan.current?.abort();
    scan.current = null;
    setScanStatus(null);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    getCapabilities(controller.signal)
      .then((capabilities) => { if (!controller.signal.aborted) setDemo(capabilities.demo_only === true); })
      .catch(() => { /* Keep the demo action disabled unless the backend enables it. */ });
    return () => {
      controller.abort();
      scan.current?.abort();
      sending.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (submitting) return;
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout>;

    async function refresh() {
      const tapAtStart = lastTap.current;
      try {
        const result = trackingId
          ? await getCheckout(trackingId, signal)
          : (await getActiveCheckout(terminalId, signal)).checkout;
        if (signal.aborted || tapAtStart !== lastTap.current || sending.current) return;
        setCheckout((previous) => result ?? (previous?.status === "WAITING_FOR_CARD" ? null : previous));
        if (trackingId && result?.status !== "WAITING_FOR_CARD") setAttempt(null);
        setLoaded(true);
        setLoadError("");
      } catch (failure) {
        if (signal.aborted || tapAtStart !== lastTap.current || sending.current) return;
        if (trackingId && failure instanceof RequestRejected && failure.status === 404) {
          setCheckout(null);
          setAttempt(null);
          setError("The checkout is no longer available. Its authorization result could not be confirmed.");
        } else {
          setLoadError(failure instanceof RequestRejected ? failure.message : "Could not reach the backend. Reconnecting…");
        }
      } finally {
        if (!signal.aborted) timer = setTimeout(refresh, 1_000);
      }
    }

    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [submitting, terminalId, trackingId]);

  useEffect(() => {
    function interruptScan() {
      if (!scan.current) return;
      stopScan();
      setError("NFC scan was interrupted. Start a new scan to try again.");
    }
    function onVisibilityChange() {
      if (document.hidden) interruptScan();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", interruptScan);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("pagehide", interruptScan);
    };
  }, [stopScan]);

  useEffect(() => {
    if (scan.current) stopScan();
  }, [readyId, stopScan]);

  async function submit(body: CheckoutTap) {
    if (sending.current) return;
    const controller = new AbortController();
    const tapRecord = { checkoutId: body.checkout_id, rejected: false };
    sending.current = controller;
    lastTap.current = tapRecord;
    stopScan();
    setCard({ checkoutId: body.checkout_id, alias: body.card_alias });
    setAttempt({ body, unknown: false });
    setError("");
    try {
      const result = await postTap(body, controller.signal);
      if (controller.signal.aborted) return;
      setCheckout(result);
      setAttempt(null);
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof RequestRejected) {
        tapRecord.rejected = true;
        setAttempt(null);
        setError(failure.message);
      } else {
        setAttempt({ body, unknown: true });
      }
    } finally {
      sending.current = null;
    }
  }

  function tap(checkoutId: string, alias: string) {
    if (sending.current || (lastTap.current?.checkoutId === checkoutId && !lastTap.current.rejected)) return;
    void submit({ terminal_id: terminalId, checkout_id: checkoutId, card_alias: alias, client_event_id: crypto.randomUUID() });
  }

  function startScan() {
    if (!reader || !readyId || scan.current || sending.current) return;
    const controller = new AbortController();
    scan.current = controller;
    setCard(null);
    setError("");
    setScanStatus("STARTING_SCAN");
    const timer = setTimeout(() => {
      stopScan();
      setError("NFC scan timed out. Start a new scan to try again.");
    }, 30_000);
    controller.signal.addEventListener("abort", () => clearTimeout(timer), { once: true });
    void scanUid(reader, controller.signal, (uid) => tap(readyId, uid), setError)
      .then(() => { if (!controller.signal.aborted) setScanStatus("WAITING_FOR_CARD"); })
      .catch((failure: Error) => {
        if (controller.signal.aborted) return;
        stopScan();
        setError(failure.message);
      });
  }

  const unknown = attempt?.unknown || checkout?.payment?.authorization_status === "UNKNOWN";
  const status = submitting ? "AUTHORIZING" : unknown ? "RESULT_UNKNOWN"
    : scanStatus ?? checkout?.status ?? (loaded ? "NO_ACTIVE_CHECKOUT" : "LOADING_CHECKOUT");

  return (
    <main className="mx-auto max-w-lg space-y-4 p-6">
      <h1 className="text-xl font-semibold">VaultPay NFC Terminal</h1>
      <p>Terminal: {terminalId}</p>
      <p>Amount: {checkout ? formatKrw.format(checkout.amount_krw) : "—"}</p>
      <div className="flex flex-wrap gap-2">
        {scanStatus
          ? <button className={button} onClick={stopScan}>Stop NFC Scan</button>
          : <button className={button} disabled={!reader || !readyId} onClick={startScan}>Start NFC Scan</button>}
        {demo && <button className={button} disabled={!readyId || !!scanStatus || !window.isSecureContext}
          onClick={() => { if (readyId) tap(readyId, DEMO_CARD); }}>Test only: Run demo tap</button>}
        {attempt?.unknown && checkout?.status === "WAITING_FOR_CARD" && (attempt.body.card_alias !== DEMO_CARD || demo) &&
          <button className={button} disabled={!!loadError} onClick={() => void submit(attempt.body)}>Retry same tap</button>}
      </div>
      <div role="status">
        <p>Status: {status}</p>
        <p>NFC scan: {scanStatus === "WAITING_FOR_CARD" ? "Active" : scanStatus ? "Starting" : "Off"}</p>
        {card && card.checkoutId === checkout?.checkout_id && <p>Card: {card.alias}</p>}
        {status === "DECLINED" && checkout?.payment?.reason_code && <p>Reason: {checkout.payment.reason_code}</p>}
      </div>
      {!reader && <p>Web NFC requires a supported Android Chrome device and a secure, top-level page.</p>}
      {attempt?.unknown && <p>Tap response could not be confirmed. Checking the original checkout…</p>}
      {(loadError || error) && <p role="alert">{loadError || error}</p>}
    </main>
  );
}
