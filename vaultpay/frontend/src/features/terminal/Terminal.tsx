import { useCallback, useEffect, useRef, useState } from "react";
import type { CheckoutRead } from "../../shared/contracts";
import { Button, cx, NfcIcon, reasonText, Spinner } from "../../ui/kit";
import {
  completeEnrollment, getActiveCheckout, getCapabilities, getCheckout, getPendingEnrollment, postTap, RequestRejected,
  type CheckoutTap, type PendingEnrollment,
} from "./api";
import { getNfcReader, scanUid } from "./nfc";

const DEMO_CARD = "demo-alice";
const formatKrw = new Intl.NumberFormat("ko-KR", { style: "currency", currency: "KRW" });
const statusText: Record<string, string> = {
  LOADING_CHECKOUT: "주문 확인 중", NO_ACTIVE_CHECKOUT: "주문 대기", STARTING_SCAN: "NFC 준비 중", WAITING_FOR_CARD: "카드 대기",
  AUTHORIZING: "승인 요청 중", RESULT_UNKNOWN: "결과 확인 중", AUTHORIZED: "승인 완료", DECLINED: "결제 거절",
  CANCELLED: "주문 취소", EXPIRED: "주문 만료",
};

export default function Terminal() {
  const [terminalId] = useState(() => new URLSearchParams(window.location.search).get("terminal_id") || "cafe-01");
  const [checkout, setCheckout] = useState<CheckoutRead | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [demo, setDemo] = useState(false);
  const [scanStatus, setScanStatus] = useState<"STARTING_SCAN" | "WAITING_FOR_CARD" | null>(null);
  const [card, setCard] = useState<{ checkoutId: string; alias: string } | null>(null);
  const [unknownCard, setUnknownCard] = useState<string | null>(null);
  const [enrollment, setEnrollment] = useState<PendingEnrollment | null>(null);
  const [enrolled, setEnrolled] = useState<PendingEnrollment | null>(null);
  const [enrollScan, setEnrollScan] = useState(false);
  const enrollScanRef = useRef<AbortController | null>(null);
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
          setError("주문을 더 이상 찾을 수 없어요. 승인 결과를 POS에서 확인해 주세요.");
        } else {
          setLoadError(failure instanceof RequestRejected ? failure.message : "노트북 서버에 연결할 수 없어요. 다시 연결하는 중…");
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
      setError("NFC 스캔이 중단됐어요. 스캔을 다시 시작해 주세요.");
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
        if (failure.code === "UNKNOWN_CARD") setUnknownCard(body.card_alias);
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
    setUnknownCard(null);
    setError("");
    setScanStatus("STARTING_SCAN");
    const timer = setTimeout(() => {
      stopScan();
      setError("30초 동안 카드가 없어서 스캔을 멈췄어요. 다시 시작해 주세요.");
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

  // Card registration mode: only while no checkout is waiting or being tracked.
  const idle = loaded && !checkout?.status?.match(/WAITING_FOR_CARD|AUTHORIZING/) && !attempt && !sending.current;
  useEffect(() => {
    if (!idle) {
      setEnrollment(null);
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await getPendingEnrollment(terminalId, controller.signal);
        if (controller.signal.aborted) return;
        setEnrollment((previous) => (previous?.enrollment_id === result.enrollment?.enrollment_id ? previous : result.enrollment));
      } catch {
        /* Keep the last known state; the checkout loop reports connectivity. */
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 1_200);
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [idle, terminalId]);

  const stopEnrollScan = useCallback(() => {
    enrollScanRef.current?.abort();
    enrollScanRef.current = null;
    setEnrollScan(false);
  }, []);

  useEffect(() => {
    if (!enrollment) stopEnrollScan();
  }, [enrollment, stopEnrollScan]);

  function startEnrollScan() {
    if (!reader || !enrollment || enrollScanRef.current) return;
    const target = enrollment;
    const controller = new AbortController();
    enrollScanRef.current = controller;
    setEnrollScan(true);
    setError("");
    const timer = setTimeout(() => { stopEnrollScan(); setError("30초 동안 카드가 없어서 스캔을 멈췄어요."); }, 30_000);
    controller.signal.addEventListener("abort", () => clearTimeout(timer), { once: true });
    let done = false;
    void scanUid(reader, controller.signal, (uid) => {
      if (done) return;
      done = true;
      stopEnrollScan();
      completeEnrollment(terminalId, target.enrollment_id, uid, new AbortController().signal)
        .then((result) => {
          if (result.status === "COMPLETED") {
            setEnrolled(result);
            setEnrollment(null);
          } else setError(result.error ?? "카드를 등록하지 못했어요.");
        })
        .catch((failure: Error) => setError(failure.message));
    }, setError).catch((failure: Error) => {
      if (controller.signal.aborted) return;
      stopEnrollScan();
      setError(failure.message);
    });
  }

  useEffect(() => {
    if (!enrolled) return;
    const timer = setTimeout(() => setEnrolled(null), 8_000);
    return () => clearTimeout(timer);
  }, [enrolled]);

  const unknown = attempt?.unknown || checkout?.payment?.authorization_status === "UNKNOWN";
  const status = submitting ? "AUTHORIZING" : unknown ? "RESULT_UNKNOWN"
    : scanStatus ?? checkout?.status ?? (loaded ? "NO_ACTIVE_CHECKOUT" : "LOADING_CHECKOUT");
  const settled = status === "AUTHORIZED" || status === "DECLINED";
  const tone = status === "AUTHORIZED" ? "bg-brand text-white" : status === "DECLINED" ? "bg-danger text-white"
    : scanStatus === "WAITING_FOR_CARD" || enrollScan ? "bg-brand-deep text-white" : "bg-canvas";
  const reason = checkout?.payment?.reason_code;

  return (
    <main className={cx("flex min-h-dvh flex-col px-5 pb-6 pt-5 transition-colors", tone)}>
      <header className="flex items-center justify-between text-sm">
        <span className="font-extrabold tracking-tight">BlockBucks 단말기</span>
        <span className="rounded-full bg-black/10 px-2.5 py-0.5 font-mono text-xs">{terminalId}</span>
      </header>

      <section className="flex flex-1 flex-col items-center justify-center text-center">
        {enrolled ? (
          <div className="animate-pop">
            <span className="mx-auto flex h-24 w-24 items-center justify-center rounded-full bg-brand text-5xl text-white">✓</span>
            <p className="mt-6 text-3xl font-extrabold">카드 등록 완료</p>
            <p className="mt-2 font-mono text-lg">{enrolled.card_alias}</p>
            <p className="mt-1 text-sm opacity-70">이제 이 카드로 결제할 수 있어요</p>
            {enrolled.engine_registration_required && <p className="mt-3 text-xs opacity-70">외부 엔진에도 카드 토큰 등록이 필요해요</p>}
          </div>
        ) : enrollment && !checkout?.status?.match(/WAITING_FOR_CARD|AUTHORIZING/) ? (
          <>
            <p className="rounded-full bg-mint px-3 py-1 text-xs font-bold text-mint-text">카드 등록 모드</p>
            <span className="relative mt-6 flex h-36 w-36 items-center justify-center">
              {enrollScan && <span className="absolute inset-0 animate-ripple rounded-full bg-white/25" />}
              <span className={cx("relative flex h-24 w-24 items-center justify-center rounded-full", enrollScan ? "bg-white text-brand-deep" : "bg-mint text-brand")}><NfcIcon className="h-12 w-12" /></span>
            </span>
            <p className="mt-6 text-2xl font-extrabold">{enrollment.customer_name ?? "고객"}님의 카드를 등록해요</p>
            <p className="mt-2 text-sm opacity-75">{enrollScan ? "휴대폰 뒷면에 카드를 대 주세요" : "아래 버튼을 눌러 스캔을 시작하세요"}</p>
            <div className="mt-6 w-full max-w-xs">
              {enrollScan
                ? <Button variant="secondary" block onClick={stopEnrollScan}>스캔 중지</Button>
                : <Button block className="py-4 text-base" disabled={!reader} onClick={startEnrollScan}>등록용 NFC 스캔 시작</Button>}
            </div>
          </>
        ) : (
          <>
            <p className="text-sm font-semibold opacity-70">결제 금액</p>
            <p className="mt-1 text-5xl font-extrabold tabular">{checkout ? formatKrw.format(checkout.amount_krw) : "—"}</p>
            <span className="relative my-8 flex h-40 w-40 items-center justify-center">
              {scanStatus === "WAITING_FOR_CARD" && <>
                <span className="absolute inset-0 animate-ripple rounded-full bg-white/25" />
                <span className="absolute inset-5 animate-ripple rounded-full bg-white/20 [animation-delay:0.6s]" />
              </>}
              <span className={cx("relative flex h-28 w-28 items-center justify-center rounded-full text-5xl",
                settled ? "bg-white" : scanStatus === "WAITING_FOR_CARD" ? "bg-white text-brand-deep" : "bg-white text-brand shadow-card",
                status === "AUTHORIZED" && "text-brand", status === "DECLINED" && "text-danger")}>
                {status === "AUTHORIZED" ? "✓" : status === "DECLINED" ? "!" : status === "AUTHORIZING" || status === "RESULT_UNKNOWN" || scanStatus === "STARTING_SCAN"
                  ? <Spinner className="h-12 w-12 border-4" /> : <NfcIcon className="h-14 w-14" />}
              </span>
            </span>
            <div role="status" className="space-y-1">
              <p className="text-2xl font-extrabold">{statusText[status] ?? status}</p>
              {status === "WAITING_FOR_CARD" && !scanStatus && <p className="text-sm opacity-70">‘NFC 스캔 시작’을 누르고 카드를 휴대폰 뒷면에 대 주세요</p>}
              {scanStatus === "WAITING_FOR_CARD" && <p className="text-sm opacity-80">카드를 휴대폰 뒷면에 대 주세요</p>}
              {status === "NO_ACTIVE_CHECKOUT" && <p className="text-sm opacity-70">POS(노트북)에서 결제 금액을 입력하면 자동으로 나타나요</p>}
              {status === "DECLINED" && reason && <p className="text-lg">{reasonText[reason] ?? reason}</p>}
              <p className="text-xs opacity-60">NFC 스캔: {scanStatus === "WAITING_FOR_CARD" ? "켜짐" : scanStatus ? "준비 중" : "꺼짐"}</p>
              {card && card.checkoutId === checkout?.checkout_id && <p className="font-mono text-sm opacity-80">카드 UID: {card.alias}</p>}
            </div>
          </>
        )}
      </section>

      <footer className="space-y-2">
        {unknownCard && (
          <div className="rounded-2xl bg-white p-4 text-left text-brand-ink shadow-card">
            <p className="text-sm font-bold">등록되지 않은 카드 UID</p>
            <p className="mt-1 select-all break-all font-mono text-xl font-bold">{unknownCard}</p>
            <p className="mt-1 text-xs text-muted">고객 앱 ‘카드 등록 → UID 입력’에 넣거나, 고객 앱에서 매장 단말기 등록을 요청하세요.</p>
          </div>
        )}
        {!enrollment && !enrolled && (
          <div className="flex flex-col gap-2">
            {scanStatus
              ? <Button variant="secondary" block className="py-4 text-base" onClick={stopScan}>스캔 중지</Button>
              : <Button block className="py-4 text-base" disabled={!reader || !readyId} onClick={startScan}>NFC 스캔 시작</Button>}
            {demo && <Button variant="ghost" block disabled={!readyId || !!scanStatus || !window.isSecureContext}
              onClick={() => { if (readyId) tap(readyId, DEMO_CARD); }}>테스트용: 데모 카드 태그</Button>}
            {attempt?.unknown && checkout?.status === "WAITING_FOR_CARD" && (attempt.body.card_alias !== DEMO_CARD || demo) &&
              <Button variant="dark" block disabled={!!loadError} onClick={() => void submit(attempt.body)}>같은 태그 다시 보내기</Button>}
          </div>
        )}
        {!reader && <p className="text-center text-xs opacity-70">Web NFC는 안드로이드 Chrome에서 localhost(USB adb reverse) 또는 HTTPS로 열었을 때만 동작해요.</p>}
        {attempt?.unknown && <p className="text-center text-sm">태그 응답을 확인하지 못했어요. 같은 주문의 결과를 확인하는 중…</p>}
        {(loadError || error) && <p role="alert" className="rounded-xl bg-white/90 px-3 py-2 text-center text-sm text-danger">{loadError || error}</p>}
      </footer>
    </main>
  );
}
