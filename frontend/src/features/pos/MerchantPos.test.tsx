import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import MerchantPos from "./MerchantPos";

const checkout = {
  checkout_id: "d2ea5d23-f998-4cba-bcb4-f029e93d8117",
  amount_krw: 4500,
  status: "WAITING_FOR_CARD",
  payment: null,
};

beforeEach(() => sessionStorage.clear());

it("retries an unconfirmed creation after refresh with the same key and amount", async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json({ checkout: null }))
    .mockRejectedValueOnce(new DOMException("Response lost", "TimeoutError"))
    .mockResolvedValueOnce(Response.json(checkout))
    .mockResolvedValueOnce(Response.json({ ...checkout, status: "AUTHORIZED" }))
    .mockResolvedValueOnce(Response.json({ checkout: null }));
  vi.stubGlobal("fetch", fetch);

  const first = render(<MerchantPos />);
  const create = await screen.findByRole("button", { name: "Create Checkout" });
  fireEvent.change(screen.getByLabelText("Amount (KRW)"), { target: { value: "4500" } });
  fireEvent.click(create);
  fireEvent.click(create);
  await screen.findByRole("button", { name: "Retry Checkout Creation" });
  expect(fetch).toHaveBeenCalledTimes(2);
  first.unmount();

  render(<MerchantPos />);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText("Amount (KRW)")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Retry Checkout Creation" }));
  expect(fetch.mock.calls[2][1]).toMatchObject({
    method: "POST",
    headers: { "Idempotency-Key": fetch.mock.calls[1][1].headers["Idempotency-Key"] },
    body: JSON.stringify({ merchant_id: "cafe", terminal_id: "cafe-01", amount_krw: 4500 }),
  });
  expect(await screen.findByText("결제가 완료되었습니다.")).toBeInTheDocument();
  expect(screen.getByText(`Checkout: ${checkout.checkout_id}`)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Next Checkout" }));
  expect(await screen.findByRole("button", { name: "Create Checkout" })).toBeInTheDocument();
  expect(fetch.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(2);
});

it.each([
  ["DECLINED", "LIMIT_EXCEEDED", "결제 가능한 자산이 부족합니다."],
  ["DECLINED", "WALLET_BUSY", "다른 결제를 처리 중입니다."],
  ["DECLINED", "UNKNOWN_WALLET", "등록되지 않은 카드입니다."],
  ["DECLINED", "PRICE_UNAVAILABLE", "현재 자산 가격을 확인할 수 없습니다."],
  ["AUTHORIZING", "UNKNOWN", "결제 결과를 확인 중입니다."],
  ["AUTHORIZING", "TIMEOUT", "결제 결과를 확인 중입니다."],
])("displays the server's %s result for %s", async (status, reason, message) => {
  const result = { ...checkout, status, payment: { reason_code: reason } };
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json({ checkout: result }))
    .mockResolvedValueOnce(Response.json(result)));

  render(<MerchantPos />);
  expect(await screen.findByText(message)).toBeInTheDocument();
  expect(screen.getByText(`Authorization reason: ${reason}`)).toBeInTheDocument();
  if (status === "AUTHORIZING") {
    expect(screen.getByText("Status: PROCESSING")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next Checkout" })).toBeNull();
  } else {
    expect(screen.getByText("Status: DECLINED")).toBeInTheDocument();
  }
});
