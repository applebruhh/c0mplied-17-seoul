import type { EventRead, PaymentRead, PaymentsResponse } from "../../shared/contracts";

const paymentsPath = "/api/ops/payments";

async function get(path: string, signal: AbortSignal) {
  const response = await fetch(path, { signal, cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response;
}

export async function listPayments(signal: AbortSignal): Promise<PaymentsResponse> {
  const response = await get(paymentsPath, signal);
  const data: PaymentsResponse = await response.json();
  if (!Array.isArray(data?.items)) throw new Error("Invalid payment list");
  return data;
}

export async function getPayment(paymentId: string, signal: AbortSignal): Promise<PaymentRead> {
  const response = await get(`${paymentsPath}/${encodeURIComponent(paymentId)}`, signal);
  const data: PaymentRead = await response.json();
  if (data?.payment_id !== paymentId) throw new Error("Invalid payment details");
  return data;
}

export async function getPaymentEvents(
  checkoutId: string, paymentId: string, signal: AbortSignal,
): Promise<EventRead[]> {
  // The detail endpoint has no history. Replay the checkout outbox without a live connection.
  const path = `/api/checkouts/${encodeURIComponent(checkoutId)}/events?after=0&follow=false`;
  const response = await get(path, signal);
  if (!response.headers.get("content-type")?.startsWith("text/event-stream")) {
    throw new Error("Invalid payment event stream");
  }
  const replay = await response.text();
  const events: EventRead[] = [];

  for (const frame of replay.split(/\r?\n\r?\n/)) {
    const data = frame.split(/\r?\n/).find((line) => line.startsWith("data: "));
    if (!data) continue;
    const event: EventRead = JSON.parse(data.slice("data: ".length));
    // The replay also contains a snapshot, checkout-only events and possibly a global reset.
    if (event?.payment_id === paymentId) events.push(event);
  }

  return events;
}
