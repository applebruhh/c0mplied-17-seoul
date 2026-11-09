import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { ActiveCheckoutResponse, CheckoutCreate, CheckoutRead, ErrorResponse } from "../../shared/contracts";
import { Badge, Button, cx, formatWon, NfcIcon, Spinner } from "../../ui/kit";

const params = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
const terminalId = params.get("terminal_id") || "cafe-01";
const merchantId = params.get("merchant_id") || (terminalId.startsWith("galaxy-") ? "merchant-demo" : "cafe");
const merchantName = params.get("store") || (merchantId === "cafe" ? "BlockBucks 카페" : "VaultPay 데모 카페");
const storageKey = `vaultpay.pos.${terminalId}`;
const activeCheckoutUrl = `/api/terminals/${terminalId}/active-checkout`;
const statusText: Record<string, string> = {
  WAITING_FOR_CARD: "카드 대기", PROCESSING: "승인 확인 중", AUTHORIZED: "승인 완료", DECLINED: "거절", CANCELLED: "취소", EXPIRED: "만료",
};
const pendingMessage = "결제 결과를 확인 중입니다.";
const reasonMessages = new Map([
  ["LIMIT_EXCEEDED", "결제 가능한 자산이 부족합니다."],
  ["WALLET_BUSY", "다른 결제를 처리 중입니다."],
  ["UNKNOWN_WALLET", "등록되지 않은 카드입니다."],
  ["PRICE_UNAVAILABLE", "현재 자산 가격을 확인할 수 없습니다."],
  ["UNKNOWN", pendingMessage],
  ["TIMEOUT", pendingMessage],
]);

type PendingCreation = { kind: "pending"; key: string; amount: number };
type TrackedCheckout = { kind: "checkout"; id: string };
type PosState =
  | { kind: "checking" }
  | { kind: "ready" }
  | PendingCreation
  | (TrackedCheckout & { data?: CheckoutRead });

class CheckoutApiError extends Error {
  constructor(readonly status: number, readonly code?: string, message?: string) {
    super(message || `Checkout request failed (HTTP ${status}).`);
  }
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const timeout = AbortSignal.timeout(5000);
  const response = await fetch(url, {
    ...init,
    cache: "no-store",
    signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
  });
  if (!response.ok) {
    const body: ErrorResponse | null = await response.json().catch(() => null);
    throw new CheckoutApiError(response.status, body?.error?.code, body?.error?.message);
  }
  return response.json();
}

function initialState(): PosState {
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as PendingCreation | TrackedCheckout | null;
    if (saved?.kind === "checkout" || saved?.kind === "pending") return saved;
  } catch {
    // The terminal lookup can recover a currently active checkout.
  }
  return { kind: "checking" };
}

function track(checkout: CheckoutRead): PosState {
  const reference: TrackedCheckout = { kind: "checkout", id: checkout.checkout_id };
  sessionStorage.setItem(storageKey, JSON.stringify(reference));
  return { ...reference, data: checkout };
}

function isActive(checkout: CheckoutRead) {
  return checkout.status === "WAITING_FOR_CARD" || checkout.status === "AUTHORIZING";
}

export default function MerchantPos() {
  const [state, setState] = useState(initialState);
  const [amount, setAmount] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const creationInFlight = useRef(false);
  const checkoutId = state.kind === "checkout" ? state.id : null;
  const checkout = state.kind === "checkout" ? state.data : undefined;
  const finished = checkout !== undefined && !isActive(checkout);
  const checking = state.kind === "checking";

  useEffect(() => {
    if ((!checking && !checkoutId) || finished) return;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const current = checkoutId
          ? await request<CheckoutRead>(`/api/checkouts/${encodeURIComponent(checkoutId)}`, { signal: controller.signal })
          : (await request<ActiveCheckoutResponse>(activeCheckoutUrl, { signal: controller.signal })).checkout;
        if (controller.signal.aborted) return;

        if (!checkoutId) {
          setState(current ? track(current) : { kind: "ready" });
        } else if (current) {
          setState({ kind: "checkout", id: checkoutId, data: current });
          if (isActive(current)) timer = setTimeout(refresh, 1000);
        }
        setError("");
      } catch (error) {
        if (controller.signal.aborted) return;
        if (checkoutId && error instanceof CheckoutApiError && error.code === "CHECKOUT_NOT_FOUND") {
          sessionStorage.removeItem(storageKey);
          setState({ kind: "checking" });
          setError("");
          return;
        }
        setError(checkoutId ? pendingMessage : "단말기 상태를 확인하지 못했어요. 다시 시도하는 중…");
        timer = setTimeout(refresh, 1000);
      }
    }

    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [checking, checkoutId, finished]);

  async function createCheckout(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creationInFlight.current || (state.kind !== "ready" && state.kind !== "pending")) return;

    const amountKrw = state.kind === "pending" ? state.amount : Number(amount);
    if (!Number.isInteger(amountKrw) || amountKrw < 1 || amountKrw > 10_000_000) {
      setError("1원부터 10,000,000원까지 정수로 입력해 주세요.");
      return;
    }

    creationInFlight.current = true;
    setSubmitting(true);
    setError("");
    try {
      // Save the key and amount before sending a request whose response might be lost.
      const attempt: PendingCreation = state.kind === "pending"
        ? state
        : { kind: "pending", key: crypto.randomUUID(), amount: amountKrw };
      sessionStorage.setItem(storageKey, JSON.stringify(attempt));
      setState(attempt);
      const body: CheckoutCreate = { merchant_id: merchantId, terminal_id: terminalId, amount_krw: attempt.amount };
      const created = await request<CheckoutRead>("/api/checkouts", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": attempt.key },
        body: JSON.stringify(body),
      });
      setState(track(created));
    } catch (error) {
      if (error instanceof CheckoutApiError && error.code === "ACTIVE_CHECKOUT_EXISTS") {
        sessionStorage.removeItem(storageKey);
        setState({ kind: "checking" });
      } else if (error instanceof CheckoutApiError &&
        (error.status === 422 || error.code === "UNKNOWN_TERMINAL" || error.code === "MERCHANT_MISMATCH")) {
        sessionStorage.removeItem(storageKey);
        setState({ kind: "ready" });
        setError(error.message);
      } else {
        setError("주문 생성 결과를 확인하지 못했어요. 같은 주문으로 다시 시도해 주세요.");
      }
    } finally {
      creationInFlight.current = false;
      setSubmitting(false);
    }
  }

  async function cancelCheckout() {
    if (!checkoutId) return;
    try {
      const cancelled = await request<CheckoutRead>(`/api/checkouts/${encodeURIComponent(checkoutId)}/cancel`, { method: "POST" });
      setState({ kind: "checkout", id: checkoutId, data: cancelled });
    } catch (error) {
      setError(error instanceof CheckoutApiError ? error.message : "주문을 취소하지 못했어요.");
    }
  }

  function nextCheckout() {
    sessionStorage.removeItem(storageKey);
    setAmount("");
    setError("");
    setState({ kind: "checking" });
  }

  const reason = checkout?.payment?.reason_code;
  const authorizationError = checkout?.status === "DECLINED"
    ? reason
    : checkout?.status === "AUTHORIZING" ? reason ?? checkout.payment?.last_error : null;
  let message: string | undefined;
  if (checkout?.status === "AUTHORIZED") {
    message = "결제가 완료되었습니다.";
  } else if (checkout?.status === "DECLINED") {
    message = (reason && reasonMessages.get(reason)) || "결제가 거절되었습니다.";
  } else if (checkout?.status === "AUTHORIZING" &&
    (checkout.payment?.authorization_status === "UNKNOWN" || authorizationError)) {
    message = pendingMessage;
  }

  const entering = state.kind === "ready" || state.kind === "pending";
  const shownAmount = state.kind === "pending" ? state.amount : Number(amount) || 0;
  const status = checkout?.status;
  const statusLabel = status === "AUTHORIZING" ? "PROCESSING" : status;

  function press(key: string) {
    if (state.kind !== "ready" || submitting) return;
    setAmount((current) => {
      if (key === "C") return "";
      if (key === "⌫") return current.slice(0, -1);
      const next = (current + key).replace(/^0+/, "");
      return Number(next) > 10_000_000 ? current : next;
    });
  }

  return (
    <main className="min-h-[calc(100vh-64px)] bg-canvas">
      <div className="mx-auto grid max-w-6xl gap-6 px-6 py-8 lg:grid-cols-[1fr_1.15fr]">
        <section className="rounded-3xl bg-white p-6 shadow-card">
          <div className="mb-5 flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-extrabold">{merchantName}</h1>
              <p className="text-sm text-muted">가맹점 {merchantId} · 단말기 {terminalId}</p>
            </div>
            <Badge>POS</Badge>
          </div>
          {checking && <p role="status" className="flex items-center gap-2 py-10 text-muted"><Spinner />단말기 상태 확인 중…</p>}
          {entering && (
            <form onSubmit={createCheckout}>
              <label className="block text-sm font-semibold text-muted" htmlFor="pos-amount">결제 금액 (원)</label>
              <input
                id="pos-amount"
                                className="mt-2 block w-full rounded-2xl bg-canvas px-5 py-4 text-right text-4xl font-extrabold tabular outline-none ring-brand focus:ring-2 disabled:text-muted"
                type="number"
                min={1}
                max={10_000_000}
                step={1}
                required
                value={state.kind === "pending" ? state.amount : amount}
                disabled={state.kind === "pending" || submitting}
                onChange={(event) => setAmount(event.target.value)}
              />
              <div className="mt-3 flex flex-wrap gap-2">
                {[4500, 5900, 12000, 30000].map((preset) => (
                  <button key={preset} type="button" disabled={state.kind !== "ready"} onClick={() => setAmount(String(preset))}
                    className="rounded-full bg-mint px-3.5 py-1.5 text-sm font-semibold text-brand-deep hover:bg-[#DCEEE6] disabled:opacity-50">{formatWon(preset)}</button>
                ))}
              </div>
              <div className="mt-4 grid grid-cols-3 gap-2">
                {["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "⌫"].map((key) => (
                  <button key={key} type="button" onClick={() => press(key)} disabled={state.kind !== "ready"} aria-label={key === "⌫" ? "지우기" : key === "C" ? "초기화" : key}
                    className="rounded-2xl bg-canvas py-4 text-2xl font-bold hover:bg-[#E4EAE7] disabled:opacity-40">{key}</button>
                ))}
              </div>
              <Button type="submit" block className="mt-4 py-5 text-lg" disabled={submitting}>
                {submitting ? <><Spinner />주문 생성 중…</> : state.kind === "pending" ? "같은 주문 다시 보내기" : `${formatWon(shownAmount)} 결제 요청`}
              </Button>
            </form>
          )}
          {state.kind === "pending" && !submitting && (
            <p role="status" className="mt-3 text-sm text-warn">주문 생성이 확인되지 않았어요. 같은 주문 키로 다시 보내면 중복 없이 복구돼요.</p>
          )}
          {checkoutId && (
            <div className="space-y-3">
              <div className="rounded-2xl bg-canvas p-5">
                <p className="text-sm text-muted">결제 금액</p>
                <p className="text-4xl font-extrabold tabular">{checkout ? formatWon(checkout.amount_krw) : "—"}</p>
              </div>
              <dl className="space-y-1 text-sm">
                <div className="flex justify-between"><dt className="text-muted">주문 ID</dt><dd className="break-all font-mono text-xs">{checkoutId}</dd></div>
                <div className="flex justify-between"><dt className="text-muted">상태</dt><dd className="font-semibold">{statusLabel ? statusText[statusLabel] ?? statusLabel : "…"}</dd></div>
                {authorizationError && <div className="flex justify-between"><dt className="text-muted">사유</dt><dd className="font-mono">{authorizationError}</dd></div>}
              </dl>
              {status === "WAITING_FOR_CARD" && <Button variant="ghost" block onClick={() => void cancelCheckout()}>주문 취소</Button>}
              {finished && <Button block className="py-4 text-base" onClick={nextCheckout}>다음 주문</Button>}
            </div>
          )}
          {error && error !== message && <p role="status" className="mt-3 text-sm text-danger">{error}</p>}
        </section>

        <section aria-live="polite" className={cx("flex min-h-[520px] flex-col items-center justify-center rounded-3xl p-8 text-center shadow-card transition-colors",
          status === "AUTHORIZED" ? "bg-brand text-white" : status === "DECLINED" ? "bg-danger text-white"
            : status === "WAITING_FOR_CARD" ? "bg-brand-deep text-white" : "bg-white")}>
          {!checkout && (
            <>
              <span className="flex h-24 w-24 items-center justify-center rounded-full bg-mint text-brand"><NfcIcon className="h-12 w-12" /></span>
              <p className="mt-6 text-2xl font-bold">금액을 입력하고 결제를 요청하세요</p>
              <p className="mt-2 text-muted">요청하면 갤럭시 단말기가 카드 태그를 기다려요.</p>
            </>
          )}
          {status === "WAITING_FOR_CARD" && (
            <>
              <span className="relative flex h-36 w-36 items-center justify-center">
                <span className="absolute inset-0 animate-ripple rounded-full bg-white/25" />
                <span className="absolute inset-4 animate-ripple rounded-full bg-white/20 [animation-delay:0.6s]" />
                <span className="relative flex h-24 w-24 items-center justify-center rounded-full bg-white text-brand-deep"><NfcIcon className="h-12 w-12" /></span>
              </span>
              <p className="mt-8 text-5xl font-extrabold tabular">{formatWon(checkout?.amount_krw)}</p>
              <p className="mt-3 text-xl font-semibold">카드를 단말기 뒷면에 대 주세요</p>
              <p className="mt-1 text-sm text-white/70">갤럭시 단말기에서 NFC 스캔이 켜져 있어야 해요</p>
            </>
          )}
          {status === "AUTHORIZING" && (
            <>
              <Spinner className="h-16 w-16 border-4 text-brand" />
              <p className="mt-6 text-3xl font-bold">승인 확인 중</p>
              <p className="mt-2 text-muted">{message ?? "결제 엔진의 응답을 기다리고 있어요."}</p>
            </>
          )}
          {status === "AUTHORIZED" && (
            <div className="animate-pop">
              <span className="mx-auto flex h-24 w-24 items-center justify-center rounded-full bg-white text-5xl text-brand">✓</span>
              <p className="mt-6 text-sm font-semibold uppercase tracking-[0.2em] text-white/70">Approved</p>
              <p className="mt-1 text-5xl font-extrabold">승인 완료</p>
              <p className="mt-3 text-4xl font-bold tabular">{formatWon(checkout?.amount_krw)}</p>
              <p className="mt-4 text-lg" lang="ko">{message}</p>
            </div>
          )}
          {status === "DECLINED" && (
            <div className="animate-pop">
              <span className="mx-auto flex h-24 w-24 items-center justify-center rounded-full bg-white text-5xl text-danger">!</span>
              <p className="mt-6 text-5xl font-extrabold">결제 거절</p>
              <p className="mt-4 text-2xl" lang="ko">{message}</p>
              <p className="mt-2 text-sm text-white/70">사유 코드 {reason ?? "—"}</p>
            </div>
          )}
          {(status === "CANCELLED" || status === "EXPIRED") && (
            <>
              <p className="text-3xl font-bold">{status === "CANCELLED" ? "주문이 취소됐어요" : "주문 시간이 지났어요"}</p>
              <p className="mt-2 text-muted">다음 주문을 눌러 새로 시작하세요.</p>
            </>
          )}
          <p className="mt-10 text-xs opacity-60">POS에는 승인 결과만 표시돼요 · 코인 매도·정산은 운영 화면에서 확인</p>
        </section>
      </div>
    </main>
  );
}
