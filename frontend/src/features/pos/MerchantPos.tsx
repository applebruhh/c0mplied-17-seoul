import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { ActiveCheckoutResponse, CheckoutCreate, CheckoutRead, ErrorResponse } from "../../shared/contracts";

const merchantId = "cafe";
const terminalId = "cafe-01";
const storageKey = `vaultpay.pos.${terminalId}`;
const activeCheckoutUrl = `/api/terminals/${terminalId}/active-checkout`;
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
        setError(checkoutId ? pendingMessage : "Could not check this terminal. Retrying…");
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
      setError("Enter a whole KRW amount from 1 to 10,000,000.");
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
        setError("Could not confirm checkout creation.");
      }
    } finally {
      creationInFlight.current = false;
      setSubmitting(false);
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

  return (
    <main className="mx-auto max-w-6xl space-y-4 px-6 py-12">
      <h1 className="text-2xl font-semibold">VaultPay Merchant POS</h1>
      {checking && <p role="status">Checking terminal…</p>}
      {(state.kind === "ready" || state.kind === "pending") && (
        <form onSubmit={createCheckout} className="flex flex-wrap items-end gap-3">
          <label className="block">
            Amount (KRW)
            <input
              className="block rounded border border-gray-400 px-3 py-2 disabled:bg-gray-100"
              type="number"
              min={1}
              max={10_000_000}
              step={1}
              required
              value={state.kind === "pending" ? state.amount : amount}
              disabled={state.kind === "pending" || submitting}
              onChange={(event) => setAmount(event.target.value)}
            />
          </label>
          <button className="rounded border border-gray-500 px-3 py-2 disabled:opacity-50" disabled={submitting}>
            {submitting ? "Creating Checkout…" : state.kind === "pending" ? "Retry Checkout Creation" : "Create Checkout"}
          </button>
        </form>
      )}
      {state.kind === "pending" && !submitting && (
        <p role="status">Checkout creation is unconfirmed. Retry to recover the same checkout request.</p>
      )}
      {checkoutId && (
        <div role="status" className="space-y-2">
          <p className="break-all">Checkout: {checkoutId}</p>
          {checkout ? (
            <>
              <p>Amount: {checkout.amount_krw.toLocaleString("ko-KR")} KRW</p>
              <p>Status: {checkout.status === "AUTHORIZING" ? "PROCESSING" : checkout.status}</p>
              {message && <p lang="ko">{message}</p>}
              {authorizationError && <p>Authorization reason: {authorizationError}</p>}
            </>
          ) : <p>Loading checkout status…</p>}
        </div>
      )}
      {error && error !== message && <p role="status" lang={error === pendingMessage ? "ko" : undefined}>{error}</p>}
      {finished && (
        <button className="rounded border border-gray-500 px-3 py-2" onClick={nextCheckout}>Next Checkout</button>
      )}
    </main>
  );
}
