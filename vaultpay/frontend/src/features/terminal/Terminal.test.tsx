import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Terminal from "./Terminal";
import { normalizeUid } from "./nfc";

const CHECKOUT_ID = "db118202-f9f6-47d9-9016-f622812d388a";
const checkout = { checkout_id: CHECKOUT_ID, terminal_id: "cafe-01", amount_krw: 4500, status: "WAITING_FOR_CARD" };
const authorized = { ...checkout, status: "AUTHORIZED" };

function mockBackend(demoOnly: boolean, tap: () => Promise<Response>, readCheckout = async () => Response.json(checkout)) {
  const fetch = vi.fn(async (path: string, _options?: RequestInit) => {
    if (path === "/api/capabilities") return Response.json({ demo_only: demoOnly });
    if (path === "/api/terminals/cafe-01/active-checkout") return Response.json({ checkout });
    if (path === `/api/checkouts/${CHECKOUT_ID}`) return readCheckout();
    if (path === "/api/nfc/tap") return tap();
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("isSecureContext", true);
  return () => fetch.mock.calls.filter(([path]) => path === "/api/nfc/tap").map(([, options]) => options);
}

describe("NFC UID normalization", () => {
  it("keeps the UID byte order and rejects unavailable or invalid values", () => {
    expect(normalizeUid("04:a1-b2 c3:\td4:e5:f6")).toBe("04A1B2C3D4E5F6");
    for (const serial of [undefined, "", " : ", "04:a", "04:xy"]) expect(normalizeUid(serial)).toBeNull();
  });
});

describe("terminal tap delivery", () => {
  it("ignores missing UIDs and submits exactly once for repeated reads", async () => {
    const taps = mockBackend(false, async () => Response.json({ checkout: authorized }));
    let reader!: EventTarget;
    vi.stubGlobal("NDEFReader", class extends EventTarget {
      constructor() {
        super();
        reader = this;
      }
      async scan() {}
    });
    render(<Terminal />);
    await screen.findByText("₩4,500");
    expect(screen.queryByText("테스트용: 데모 카드 태그")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "NFC 스캔 시작" }));
    await screen.findByText("NFC 스캔: 켜짐");

    act(() => { reader.dispatchEvent(Object.assign(new Event("reading"), { serialNumber: "", message: "demo-alice" })); });
    expect(taps()).toHaveLength(0);
    expect(screen.getByRole("alert")).toHaveTextContent("올바른 NFC UID");

    act(() => {
      for (let read = 0; read < 3; read++) {
        reader.dispatchEvent(Object.assign(new Event("reading"), { serialNumber: "04:a1:b2:c3:d4:e5:f6" }));
      }
    });
    await screen.findByText("승인 완료");
    expect(screen.getByText("카드 UID: 04A1B2C3D4E5F6")).toBeVisible();
    expect(taps()).toHaveLength(1);
    expect(taps()[0]).toMatchObject({ method: "POST", headers: { "Content-Type": "application/json" } });
    expect(JSON.parse(String(taps()[0]?.body))).toEqual({
      terminal_id: "cafe-01", checkout_id: CHECKOUT_ID, card_alias: "04A1B2C3D4E5F6",
      client_event_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    });
  });

  it("keeps a timed-out demo tap unknown and retries with the same event and checkout", async () => {
    const taps = mockBackend(true, vi.fn<() => Promise<Response>>()
      .mockResolvedValueOnce(new Response(null, { status: 504 }))
      .mockResolvedValueOnce(Response.json({ checkout: authorized })));
    render(<Terminal />);
    await screen.findByText("₩4,500");
    fireEvent.click(await screen.findByRole("button", { name: "테스트용: 데모 카드 태그" }));
    await screen.findByText("결과 확인 중");
    fireEvent.click(screen.getByRole("button", { name: "같은 태그 다시 보내기" }));
    await screen.findByText("승인 완료");
    expect(taps()).toHaveLength(2);
    expect(JSON.parse(String(taps()[0]?.body)).card_alias).toBe("demo-alice");
    expect(taps()[0]?.body).toBe(taps()[1]?.body);
  });

  it("checks the original checkout after the backend reports an unknown authorization", async () => {
    const pending = { ...checkout, status: "AUTHORIZING", payment: { authorization_status: "UNKNOWN" } };
    let confirm!: (response: Response) => void;
    const result = new Promise<Response>((resolve) => { confirm = resolve; });
    const readCheckout = vi.fn(() => result);
    const taps = mockBackend(true, async () => Response.json({ checkout: pending }, { status: 202 }), readCheckout);
    render(<Terminal />);
    await screen.findByText("₩4,500");
    fireEvent.click(await screen.findByRole("button", { name: "테스트용: 데모 카드 태그" }));
    await screen.findByText("결과 확인 중");
    expect(readCheckout).toHaveBeenCalledOnce();
    confirm(Response.json({ ...checkout, status: "DECLINED", payment: { reason_code: "LIMIT_EXCEEDED" } }));
    await screen.findByText("결제 거절");
    expect(taps()).toHaveLength(1);
  });
});
