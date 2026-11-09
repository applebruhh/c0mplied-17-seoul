import { useEffect, useRef, useState } from "react";
import { api, Badge, Button, Card, errorText, NfcIcon, Notice, Row, Spinner } from "../../ui/kit";
import { getNfcReader, normalizeUid, scanUid } from "../terminal/nfc";
import type { Enrollment, Me } from "./types";

type Method = "terminal" | "phone" | "manual";

export function CardRegister({ me, onRegistered }: { me: Me; onRegistered: (me: Me, uid: string, token: string | null) => void }) {
  const reader = getNfcReader();
  const [method, setMethod] = useState<Method>("terminal");
  const [terminalId, setTerminalId] = useState("cafe-01");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [uid, setUid] = useState("");
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const scan = useRef<AbortController | null>(null);
  const customerId = me.profile.customer_id;

  useEffect(() => () => scan.current?.abort(), []);

  // Follow the terminal enrollment until the Galaxy reader binds a card.
  useEffect(() => {
    if (!enrollment || enrollment.status !== "PENDING") return;
    let timer: ReturnType<typeof setTimeout>;
    let active = true;
    async function poll() {
      try {
        const next = await api<Enrollment>(`/api/app/card-enrollments/${enrollment!.enrollment_id}`);
        if (!active) return;
        if (next.status === "COMPLETED" && next.card_alias) {
          // Load the profile before changing state: a state change ends this poll.
          const fresh = await api<Me>(`/api/app/users/${encodeURIComponent(customerId)}`);
          if (active) onRegistered(fresh, next.card_alias, next.card_token);
          return;
        }
        if (next.status !== "PENDING") {
          setEnrollment(next);
          return;
        }
      } catch (failure) {
        if (active) setError(errorText(failure));
      }
      if (active) timer = setTimeout(poll, 1000);
    }
    timer = setTimeout(poll, 800);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [enrollment, customerId, onRegistered]);

  async function startTerminal() {
    setBusy(true);
    setError("");
    try {
      setEnrollment(await api<Enrollment>(`/api/app/users/${encodeURIComponent(customerId)}/card-enrollments`, {
        method: "POST", json: { terminal_id: terminalId.trim() || "cafe-01" },
      }));
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  async function cancelTerminal() {
    if (!enrollment) return;
    await api(`/api/app/card-enrollments/${enrollment.enrollment_id}/cancel`, { method: "POST" }).catch(() => undefined);
    setEnrollment(null);
  }

  async function bind(raw: string) {
    const normalized = normalizeUid(raw);
    if (!normalized) {
      setError("UID는 04:A1:B2… 처럼 16진수 바이트여야 해요.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await api<Me & { card_alias: string; card_token: string }>(`/api/app/users/${encodeURIComponent(customerId)}/cards`, {
        method: "POST", json: { uid: normalized },
      });
      onRegistered(result, result.card_alias, result.card_token);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  function phoneScan() {
    if (!reader || scan.current) return;
    const controller = new AbortController();
    scan.current = controller;
    setScanning(true);
    setError("");
    const stop = () => {
      controller.abort();
      scan.current = null;
      setScanning(false);
    };
    const timer = setTimeout(() => { stop(); setError("스캔 시간이 지났어요. 다시 시도해 주세요."); }, 30_000);
    void scanUid(reader, controller.signal, (read) => { clearTimeout(timer); stop(); void bind(read); }, setError)
      .catch((failure: Error) => { clearTimeout(timer); stop(); setError(failure.message); });
  }

  const tabs: Array<[Method, string]> = [["terminal", "매장 단말기"], ["phone", "이 폰으로"], ["manual", "UID 입력"]];

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-1 rounded-xl bg-[#E4EAE7] p-1 text-[13px] font-semibold" role="tablist">
        {tabs.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={method === id} onClick={() => { setMethod(id); setError(""); }}
            className={method === id ? "rounded-lg bg-white py-2 text-brand-deep shadow-sm" : "py-2 text-muted"}>{label}</button>
        ))}
      </div>

      {method === "terminal" && (
        <Card>
          {!enrollment || enrollment.status !== "PENDING" ? (
            <>
              <div className="flex flex-col items-center py-3 text-center">
                <span className="flex h-14 w-14 items-center justify-center rounded-full bg-mint text-brand"><NfcIcon className="h-7 w-7" /></span>
                <p className="mt-3 text-[15px] font-bold">매장 단말기(갤럭시)에 카드를 대서 등록해요</p>
                <p className="mt-1 text-xs leading-relaxed text-muted">등록 요청을 보내면 단말기 화면이 ‘카드 등록’ 모드로 바뀌어요.<br />결제 대기 중인 주문이 없을 때 동작해요.</p>
              </div>
              <label className="mt-2 block text-xs font-semibold text-muted" htmlFor="enroll-terminal">단말기 ID</label>
              <input id="enroll-terminal" value={terminalId} onChange={(event) => setTerminalId(event.target.value)}
                className="mt-1 w-full rounded-xl bg-canvas px-3.5 py-2.5 text-sm" />
              <Button block className="mt-3" disabled={busy} onClick={() => void startTerminal()}>{busy ? <Spinner /> : null}등록 요청 보내기</Button>
              {enrollment && enrollment.status !== "COMPLETED" && (
                <p className="mt-2 text-center text-xs text-muted">이전 요청: {enrollment.status === "EXPIRED" ? "시간 초과" : enrollment.status === "FAILED" ? enrollment.error : "취소됨"}</p>
              )}
            </>
          ) : (
            <div className="flex flex-col items-center py-4 text-center">
              <span className="relative flex h-20 w-20 items-center justify-center">
                <span className="absolute inset-0 animate-ripple rounded-full bg-brand/30" />
                <span className="relative flex h-16 w-16 items-center justify-center rounded-full bg-brand text-white"><NfcIcon className="h-8 w-8" /></span>
              </span>
              <p className="mt-4 text-[15px] font-bold">{enrollment.terminal_id} 단말기 뒷면에 카드를 대 주세요</p>
              <p className="mt-1 text-xs text-muted">2분 안에 태그해야 해요 · 등록되면 이 화면이 자동으로 넘어가요</p>
              <Button variant="ghost" className="mt-3" onClick={() => void cancelTerminal()}>요청 취소</Button>
            </div>
          )}
        </Card>
      )}

      {method === "phone" && (
        <Card>
          <div className="flex flex-col items-center py-3 text-center">
            <span className="relative flex h-20 w-20 items-center justify-center">
              {scanning && <span className="absolute inset-0 animate-ripple rounded-full bg-brand/30" />}
              <span className="relative flex h-16 w-16 items-center justify-center rounded-full bg-mint text-brand"><NfcIcon className="h-8 w-8" /></span>
            </span>
            <p className="mt-3 text-[15px] font-bold">{scanning ? "휴대폰 뒷면에 카드를 대 주세요" : "이 휴대폰의 NFC로 카드 UID를 읽어요"}</p>
            <p className="mt-1 text-xs text-muted">안드로이드 Chrome + localhost(USB 연결) 또는 HTTPS에서만 돼요.</p>
          </div>
          {reader
            ? <Button block disabled={busy} onClick={scanning ? () => { scan.current?.abort(); scan.current = null; setScanning(false); } : phoneScan}>{scanning ? "스캔 중지" : "NFC 스캔 시작"}</Button>
            : <Notice tone="amber">이 브라우저는 Web NFC를 쓸 수 없어요. ‘매장 단말기’ 방식이나 UID 입력을 이용해 주세요.</Notice>}
        </Card>
      )}

      {method === "manual" && (
        <Card>
          <label className="block text-sm font-bold" htmlFor="manual-uid">카드 UID</label>
          <p className="mt-0.5 text-xs text-muted">단말기에서 ‘등록되지 않은 카드’로 표시된 UID를 그대로 입력하세요.</p>
          <input id="manual-uid" value={uid} onChange={(event) => setUid(event.target.value)} placeholder="04:A1:B2:C3:D4:E5:F6"
            className="mt-2 w-full rounded-xl bg-canvas px-3.5 py-3 font-mono text-sm uppercase" autoCapitalize="characters" spellCheck={false} />
          <Button block className="mt-3" disabled={busy || !uid.trim()} onClick={() => void bind(uid)}>카드 등록</Button>
        </Card>
      )}

      <Card>
        <p className="text-xs font-semibold text-muted">카드 토큰 규칙</p>
        <div className="mt-2 space-y-1.5">
          <Row label="UID 처리" value={<span className="text-xs">구분자 제거 → 대문자 → SHA-256</span>} />
          <Row label="등록된 카드" value={me.cards.length ? me.cards.map((card) => card.masked).join(", ") : <Badge tone="gray">없음</Badge>} />
        </div>
      </Card>
      {error && <Notice tone="red">{error}</Notice>}
    </div>
  );
}
