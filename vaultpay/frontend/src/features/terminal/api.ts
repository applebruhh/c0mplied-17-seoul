import type { ActiveCheckoutResponse, CapabilitiesResponse, CheckoutRead, TapRequest, TapResponse } from "../../shared/contracts";

export type CheckoutTap = TapRequest & { checkout_id: string };

export class RequestRejected extends Error {
  constructor(readonly status: number, message: string, readonly code?: string) {
    super(message);
  }
}

const koreanErrors: Record<string, string> = {
  UNKNOWN_CARD: "등록되지 않은 카드예요",
  CHECKOUT_EXPIRED: "주문 시간이 지났어요. POS에서 새 주문을 만들어 주세요.",
  NO_ACTIVE_CHECKOUT: "결제 대기 중인 주문이 없어요.",
  CHECKOUT_ALREADY_CLAIMED: "이미 다른 카드로 결제 중인 주문이에요.",
  CHECKOUT_NOT_FOUND: "이 단말기의 주문이 아니에요.",
  IDEMPOTENCY_CONFLICT: "같은 태그 요청이 다른 내용으로 재사용됐어요.",
  UNKNOWN_TERMINAL: "등록되지 않은 단말기예요.",
};

async function request<T>(path: string, signal: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(8_000)]),
    cache: "no-store",
    ...(body !== undefined && {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const message = `서버가 HTTP ${response.status} 오류를 보냈어요.`;
    if (response.status === 408 || response.status >= 500) throw new Error(message);
    const detail = await response.json().catch(() => null);
    const code: string | undefined = detail?.error?.code;
    throw new RequestRejected(response.status, (code && koreanErrors[code]) ?? detail?.error?.message ?? message, code);
  }
  return response.json();
}

export function getActiveCheckout(terminalId: string, signal: AbortSignal) {
  return request<ActiveCheckoutResponse>(`/api/terminals/${encodeURIComponent(terminalId)}/active-checkout`, signal);
}

export function getCheckout(checkoutId: string, signal: AbortSignal) {
  return request<CheckoutRead>(`/api/checkouts/${encodeURIComponent(checkoutId)}`, signal);
}

export function getCapabilities(signal: AbortSignal) {
  return request<CapabilitiesResponse>("/api/capabilities", signal);
}

export async function postTap(body: CheckoutTap, signal: AbortSignal) {
  const result = await request<TapResponse>("/api/nfc/tap", signal, body);
  if (result.checkout.checkout_id !== body.checkout_id) throw new Error("Unexpected checkout in tap response.");
  return result.checkout;
}

export type PendingEnrollment = {
  enrollment_id: string; customer_name?: string; terminal_id: string; status: string; expires_at: string;
  card_alias: string | null; masked: string | null; card_token: string | null; error: string | null;
  engine_registration_required: boolean;
};

export function getPendingEnrollment(terminalId: string, signal: AbortSignal) {
  return request<{ enrollment: PendingEnrollment | null }>(`/api/terminals/${encodeURIComponent(terminalId)}/pending-enrollment`, signal);
}

export function completeEnrollment(terminalId: string, enrollmentId: string, uid: string, signal: AbortSignal) {
  return request<PendingEnrollment>(
    `/api/terminals/${encodeURIComponent(terminalId)}/enrollments/${encodeURIComponent(enrollmentId)}/complete`, signal, { uid },
  );
}
