import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { EventRead, PaymentRead } from "../../shared/contracts";
import OperationsPage from "./OperationsPage";

it("keeps merchant authorization visible when liquidation fails", async () => {
  const payment = {
    payment_id: "c87fdd3a-f88e-41b4-a1d2-a34a66506c1c",
    checkout_id: "a6162c85-eed6-46e8-8dfa-dc02ad508fb3",
    amount_krw: 4500,
    authorization_status: "AUTHORIZED",
    hold_status: "RELEASED",
    liquidation_status: "FAILED",
    recovery_status: "LOSS_ABSORBED",
    reason_code: "MOCK_SWAP_FAILED",
    reimbursement_status: "NOT_STARTED",
    source: "simulated",
  } satisfies Partial<PaymentRead>;
  const event: EventRead = {
    event_id: 12,
    event_type: "LIQUIDATION_FAILED",
    checkout_id: payment.checkout_id,
    payment_id: payment.payment_id,
    source: "simulated",
    occurred_at: "2026-01-01T00:00:00Z",
    payload: {},
  };
  const legacyPayment = {
    ...payment, payment_id: "fd3dd33b-daae-4fe0-8038-150087218a57",
    hold_status: null, recovery_status: null,
  } satisfies Partial<PaymentRead>;
  let items: Partial<PaymentRead>[] = [];
  const listPath = "/api/ops/payments";
  const fetchMock = vi.fn(async (path: string) => {
    if (path === listPath) return Response.json({ items, total: items.length });
    if (path === `${listPath}/${payment.payment_id}`) return Response.json(payment);
    if (path === `/api/checkouts/${payment.checkout_id}/events?after=0&follow=false`) {
      return new Response(
        `id: 12\nevent: LIQUIDATION_FAILED\ndata: ${JSON.stringify(event)}\n\nevent: snapshot\ndata: {}\n\n`,
        { headers: { "Content-Type": "text/event-stream" } },
      );
    }
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<OperationsPage />);
  expect(await screen.findByText("아직 결제가 없어요.")).toBeInTheDocument();

  items = [payment, legacyPayment];
  fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
  const paymentButton = await screen.findByRole("button", { name: payment.payment_id });
  const row = within(screen.getByRole("row", { name: new RegExp(payment.payment_id) }));
  for (const value of ["AUTHORIZED", "RELEASED", "FAILED", "LOSS_ABSORBED", "MOCK_SWAP_FAILED"]) {
    expect(row.getByRole("cell", { name: value })).toBeInTheDocument();
  }
  const legacyRow = within(screen.getByRole("row", { name: new RegExp(legacyPayment.payment_id) }));
  expect(legacyRow.getAllByRole("cell", { name: "—" })).toHaveLength(2);

  fireEvent.click(paymentButton);
  expect(await screen.findByText("Reimbursement status NOT_STARTED")).toBeInTheDocument();
  expect(await screen.findByText("LIQUIDATION_FAILED", { selector: "li span" })).toBeInTheDocument();
});
