import type { ActiveCheckoutResponse, CapabilitiesResponse, CheckoutRead, TapRequest, TapResponse } from "../../shared/contracts";

export type CheckoutTap = TapRequest & { checkout_id: string };

export class RequestRejected extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, signal: AbortSignal, body?: CheckoutTap): Promise<T> {
  const response = await fetch(path, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(8_000)]),
    cache: "no-store",
    ...(body && {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const message = `Backend returned HTTP ${response.status}.`;
    if (response.status === 408 || response.status >= 500) throw new Error(message);
    const detail = await response.json();
    throw new RequestRejected(response.status, detail?.error?.message ?? message);
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
