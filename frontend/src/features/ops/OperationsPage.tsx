import { useCallback, useEffect, useRef, useState } from "react";
import type { EventRead, PaymentRead, PaymentsResponse } from "../../shared/contracts";
import { getPayment, getPaymentEvents, listPayments } from "./operationsApi";

const refreshInterval = 5_000;
const requestTimeout = 10_000;
const amountFormat = new Intl.NumberFormat();
const headings = ["Payment ID", "Amount (KRW)", "Authorization", "Hold", "Liquidation", "Recovery", "Failure", "Source"];

function financialStatus(payment: PaymentRead, value?: string | null) {
  return payment.financial_status_reported === false ? "Not reported by engine" : value ?? "—";
}

function failureInfo(payment: PaymentRead) {
  return [...new Set([payment.reason_code, payment.last_error].filter(Boolean))].join(" · ") || "—";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Request failed";
}

function PaymentDetails({ payment, refreshCount }: { payment: PaymentRead; refreshCount: number }) {
  const { payment_id, checkout_id, updated_at } = payment;
  const [detail, setDetail] = useState<PaymentRead>();
  const [events, setEvents] = useState<EventRead[]>();
  const [detailError, setDetailError] = useState<string>();
  const [eventsError, setEventsError] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let active = true;
    const timeout = window.setTimeout(() => controller.abort(), requestTimeout);

    const loadDetail = getPayment(payment_id, signal).then((result) => {
      if (!active) return;
      setDetail(result);
      setDetailError(undefined);
    }).catch((error: unknown) => {
      if (active) setDetailError(signal.aborted ? "Request timed out" : errorMessage(error));
    });

    const loadEvents = getPaymentEvents(checkout_id, payment_id, signal).then((result) => {
      if (!active) return;
      setEvents(result);
      setEventsError(undefined);
    }).catch((error: unknown) => {
      if (active) setEventsError(signal.aborted ? "Request timed out" : errorMessage(error));
    });

    void Promise.all([loadDetail, loadEvents]).finally(() => window.clearTimeout(timeout));
    return () => {
      active = false;
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [payment_id, checkout_id, updated_at, refreshCount]);

  return (
    <section id="payment-details" className="mt-6 border p-4" aria-labelledby="payment-details-title">
      <h2 id="payment-details-title" className="break-all font-semibold">Payment details for {payment_id}</h2>
      {detailError ? <p role="alert">Could not load payment details ({detailError}).</p> : detail ? (
        <p>Reimbursement status {financialStatus(detail, detail.reimbursement_status)}</p>
      ) : <p role="status">Loading payment details…</p>}
      <h3 className="mt-4 font-semibold">Payment events</h3>
      {eventsError ? <p role="alert">Could not load payment events ({eventsError}).</p> :
        events === undefined ? <p role="status">Loading payment events…</p> :
        events.length === 0 ? <p>No events recorded for this payment.</p> : (
          <ol className="space-y-1 text-sm">
            {events.map((event) => (
              <li key={event.event_id}>
                <time dateTime={event.occurred_at}>{event.occurred_at}</time>{" · "}{event.event_type}
              </li>
            ))}
          </ol>
        )}
    </section>
  );
}

export default function OperationsPage() {
  const [payments, setPayments] = useState<PaymentsResponse>();
  const [error, setError] = useState<string>();
  const [refreshing, setRefreshing] = useState(true);
  const [refreshCount, setRefreshCount] = useState(0);
  const [selectedId, setSelectedId] = useState<string>();
  const pending = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setRefreshing(true);
    const timeout = window.setTimeout(() => controller.abort(), requestTimeout);

    try {
      const result = await listPayments(controller.signal);
      if (pending.current !== controller) return;
      setPayments(result);
      setError(undefined);
    } catch (error: unknown) {
      if (pending.current === controller) {
        setError(controller.signal.aborted ? "Request timed out" : errorMessage(error));
      }
    } finally {
      window.clearTimeout(timeout);
      if (pending.current === controller) {
        pending.current = null;
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, refreshInterval);
    return () => {
      window.clearInterval(timer);
      pending.current?.abort();
      pending.current = null;
    };
  }, [refresh]);

  const selected = payments?.items.find((payment) => payment.payment_id === selectedId);

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <div className="mb-4 flex flex-wrap items-center gap-4">
        <h1 className="text-2xl font-semibold">VaultPay Operations</h1>
        <button type="button" className="border px-3 py-1 disabled:opacity-50" disabled={refreshing}
          onClick={() => {
            void refresh();
            setRefreshCount((count) => count + 1);
          }}>
          Refresh
        </button>
      </div>
      {error && <p role="alert" className="mb-3">Could not refresh payments ({error}). {payments && "Showing the last loaded data."}</p>}
      {!payments ? (refreshing && <p role="status">Loading payments…</p>) : payments.items.length === 0 ? (
        <p>No payments yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse bg-white text-left text-sm">
            <caption className="pb-2 text-left">Showing {payments.items.length} of {payments.total} payments, newest first.</caption>
            <thead>
              <tr>{headings.map((heading) => <th key={heading} scope="col" className="border p-2">{heading}</th>)}</tr>
            </thead>
            <tbody>
              {payments.items.map((payment) => (
                <tr key={payment.payment_id}>
                  <th scope="row" className="border p-2 font-normal">
                    <button type="button" className="text-left underline" aria-expanded={selected?.payment_id === payment.payment_id}
                      onClick={() => setSelectedId(selectedId === payment.payment_id ? undefined : payment.payment_id)}>
                      {payment.payment_id}
                    </button>
                  </th>
                  <td className="border p-2">{amountFormat.format(payment.amount_krw)}</td>
                  <td className="border p-2">{payment.authorization_status}</td>
                  <td className="border p-2">{financialStatus(payment, payment.hold_status)}</td>
                  <td className="border p-2">{financialStatus(payment, payment.liquidation_status)}</td>
                  <td className="border p-2">{financialStatus(payment, payment.recovery_status)}</td>
                  <td className="border p-2">{failureInfo(payment)}</td>
                  <td className="border p-2">{payment.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {selected && <PaymentDetails key={selected.payment_id} payment={selected} refreshCount={refreshCount} />}
    </main>
  );
}
