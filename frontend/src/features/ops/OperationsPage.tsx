import { useCallback, useEffect, useRef, useState } from "react";
import type { EventRead, PaymentRead, PaymentsResponse } from "../../shared/contracts";
import { AssetIcon, Badge, Button, cx, formatTime, formatWon, Spinner } from "../../ui/kit";
import { getOverview, getPayment, getPaymentEvents, listPayments, type Overview } from "./operationsApi";

const refreshInterval = 2_000;
const requestTimeout = 10_000;
const amountFormat = new Intl.NumberFormat("ko-KR");
const headings = ["결제 ID", "금액", "승인", "지갑 잠금", "청산", "회수", "실패 사유", "출처"];

function financialStatus(payment: PaymentRead, value?: string | null) {
  return payment.financial_status_reported === false ? "엔진 미보고" : value ?? "—";
}

function failureInfo(payment: PaymentRead) {
  return [...new Set([payment.reason_code, payment.last_error].filter(Boolean))].join(" · ") || "—";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "요청 실패";
}

function tone(value: string): "green" | "red" | "gray" | "amber" {
  if (["AUTHORIZED", "COMPLETED", "REIMBURSED", "RELEASED", "CONFIRMED"].includes(value)) return "green";
  if (["DECLINED", "FAILED", "LOSS_ABSORBED", "EXPIRED", "RECOVERY_REQUIRED"].includes(value)) return "red";
  if (["LOCKED", "PENDING", "PROCESSING", "UNKNOWN"].includes(value)) return "amber";
  return "gray";
}

function StatusCell({ value }: { value: string }) {
  return <td className="border-b border-[#EEF1EF] px-3 py-2.5" aria-label={value}>{value === "—" ? <span className="text-muted">—</span> : <Badge tone={tone(value)}>{value}</Badge>}</td>;
}

type Submission = { submission_id: string; asset: string; attempt: number; input_krw: number; status: string; quoted_slippage?: string; reason?: string };

function PaymentDetails({ payment, refreshCount, onClose }: { payment: PaymentRead; refreshCount: number; onClose: () => void }) {
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
      if (active) setDetailError(signal.aborted ? "시간 초과" : errorMessage(error));
    });

    const loadEvents = getPaymentEvents(checkout_id, payment_id, signal).then((result) => {
      if (!active) return;
      setEvents(result);
      setEventsError(undefined);
    }).catch((error: unknown) => {
      if (active) setEventsError(signal.aborted ? "시간 초과" : errorMessage(error));
    });

    void Promise.all([loadDetail, loadEvents]).finally(() => window.clearTimeout(timeout));
    return () => {
      active = false;
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [payment_id, checkout_id, updated_at, refreshCount]);

  const submissions = ((detail?.mock_asset_metadata ?? {}) as { submissions?: Submission[] }).submissions ?? [];
  const axes: Array<[string, string | null | undefined]> = detail ? [
    ["Authorization", detail.authorization_status], ["Hold", financialStatus(detail, detail.hold_status)],
    ["Liquidation", financialStatus(detail, detail.liquidation_status)], ["Recovery", financialStatus(detail, detail.recovery_status)],
  ] : [];

  return (
    <section id="payment-details" className="rounded-2xl bg-white p-5 shadow-card" aria-labelledby="payment-details-title">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold text-muted">결제 상세</p>
          <h2 id="payment-details-title" className="break-all font-mono text-sm font-semibold">{payment_id}</h2>
        </div>
        <button type="button" onClick={onClose} className="rounded-lg px-2 py-1 text-muted hover:bg-black/5" aria-label="상세 닫기">✕</button>
      </div>
      {detailError ? <p role="alert" className="mt-3 text-sm text-danger">상세를 불러오지 못했어요 ({detailError}).</p> : detail ? (
        <>
          <p className="mt-3 text-2xl font-extrabold tabular">{formatWon(detail.amount_krw)}</p>
          <p className="text-xs text-muted">고객 {detail.customer_id} · 가맹점 {detail.merchant_id} · 단말기 {detail.terminal_id}</p>
          <div className="mt-4 grid grid-cols-2 gap-2">
            {axes.map(([label, value]) => (
              <div key={label} className="rounded-xl bg-canvas px-3 py-2.5">
                <p className="text-[11px] text-muted">{label}</p>
                <Badge tone={tone(value ?? "")} className="mt-1">{value ?? "—"}</Badge>
              </div>
            ))}
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted">필요액 (여유율 포함)</dt><dd className="text-right tabular">{formatWon(detail.required_krw)}</dd>
            <dt className="text-muted">미회수 노출</dt><dd className="text-right tabular">{formatWon(detail.unpaid_exposure_krw ?? 0)}</dd>
            <dt className="text-muted">손실 흡수</dt><dd className="text-right tabular">{formatWon(detail.loss_absorbed_krw ?? 0)}</dd>
            <dt className="text-muted">차액 반환</dt><dd className="text-right tabular">{detail.excess_returned_usdc ? `${detail.excess_returned_usdc} USDC` : "—"}</dd>
          </dl>
          <p className="mt-2 text-xs text-muted">Reimbursement status {financialStatus(detail, detail.reimbursement_status)}</p>
          {submissions.length > 0 && (
            <>
              <h3 className="mt-4 text-sm font-bold">스왑 내역</h3>
              <ol className="mt-2 space-y-1.5">
                {submissions.map((leg, index) => (
                  <li key={leg.submission_id} className="flex items-center gap-2 rounded-xl bg-canvas px-3 py-2 text-xs">
                    <AssetIcon asset={leg.asset} size={22} />
                    <span className="flex-1">Leg {index + 1}: {leg.asset === "USDC" ? "USDC → Treasury" : `${leg.asset} → USDC`} · {formatWon(leg.input_krw)} · 시도 {leg.attempt}{leg.reason ? ` · ${leg.reason}` : ""}</span>
                    <Badge tone={tone(leg.status)}>{leg.status}</Badge>
                  </li>
                ))}
              </ol>
            </>
          )}
        </>
      ) : <p role="status" className="mt-3 flex items-center gap-2 text-sm text-muted"><Spinner />상세 불러오는 중…</p>}
      <h3 className="mt-5 text-sm font-bold">이벤트 타임라인</h3>
      {eventsError ? <p role="alert" className="text-sm text-danger">이벤트를 불러오지 못했어요 ({eventsError}).</p> :
        events === undefined ? <p role="status" className="text-sm text-muted">이벤트 불러오는 중…</p> :
        events.length === 0 ? <p className="text-sm text-muted">기록된 이벤트가 없어요.</p> : (
          <ol className="mt-2 space-y-0 border-l-2 border-mint-line pl-4 text-xs">
            {events.map((event) => (
              <li key={event.event_id} className="relative py-1">
                <span className="absolute -left-[21px] top-2 h-2 w-2 rounded-full bg-brand" aria-hidden />
                <time dateTime={event.occurred_at} className="text-muted">{new Date(event.occurred_at).toLocaleTimeString("ko-KR")}</time>
                {" · "}<span className="font-mono font-semibold">{event.event_type}</span>
              </li>
            ))}
          </ol>
        )}
    </section>
  );
}

function Kpi({ label, value, hint, tone: kind = "plain" }: { label: string; value: string; hint?: string; tone?: "plain" | "alert" | "dark" }) {
  return (
    <div className={cx("rounded-2xl p-4 shadow-card", kind === "dark" ? "bg-brand-deep text-white" : kind === "alert" ? "bg-danger-pale" : "bg-white")}>
      <p className={cx("text-xs", kind === "dark" ? "text-white/70" : "text-muted")}>{label}</p>
      <p className={cx("mt-1 text-2xl font-extrabold tabular", kind === "alert" && "text-danger")}>{value}</p>
      {hint && <p className={cx("mt-0.5 text-[11px]", kind === "dark" ? "text-white/60" : "text-muted")}>{hint}</p>}
    </div>
  );
}

export default function OperationsPage() {
  const [payments, setPayments] = useState<PaymentsResponse>();
  const [overview, setOverview] = useState<Overview>();
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
      const [result, summary] = await Promise.all([
        listPayments(controller.signal),
        getOverview(controller.signal).catch(() => undefined),
      ]);
      if (pending.current !== controller) return;
      setPayments(result);
      if (summary) setOverview(summary);
      setError(undefined);
    } catch (error: unknown) {
      if (pending.current === controller) {
        setError(controller.signal.aborted ? "시간 초과" : errorMessage(error));
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
    <main className="mx-auto max-w-7xl px-6 py-8">
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <div className="flex-1">
          <h1 className="text-2xl font-extrabold">운영 콘솔</h1>
          <p className="text-sm text-muted">결제 1건당 4개 상태 축(승인 · 지갑 잠금 · 청산 · 회수)과 고객 지갑을 실시간으로 봐요. 2초마다 갱신.</p>
        </div>
        {overview && <Badge tone={overview.engine_mode === "external" ? "amber" : "green"}>{overview.engine_mode === "external" ? "외부 엔진" : "데모 시뮬레이션"}</Badge>}
        <Button variant="secondary" disabled={refreshing && !payments}
          onClick={() => {
            void refresh();
            setRefreshCount((count) => count + 1);
          }}>
          새로고침
        </Button>
      </div>

      {overview && (
        <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <Kpi tone="dark" label="오늘 승인액" value={formatWon(overview.approved_today_krw)} hint={`승인 ${overview.authorized_total}건 · 거절 ${overview.declined_total}건`} />
          <Kpi label="정산 진행 중" value={`${overview.settlement_pending}건`} hint={`완료 ${overview.settlement_completed}건`} />
          <Kpi label="잠긴 지갑" value={`${overview.locked_wallets}개`} hint="결제 정산 중" />
          <Kpi tone={overview.recovery_required ? "alert" : "plain"} label="회수 필요 · 손실" value={`${overview.recovery_required}건`} hint="회사 부담 처리 대상" />
          <Kpi label="회사 결제 여력" value={formatWon(overview.company_capacity_krw)} hint="가맹점 선지급 가능액" />
          <Kpi label="가입 고객" value={`${overview.customers_total}명`} hint={`승인 대기 ${overview.pending_authorization}건`} />
        </div>
      )}

      {error && <p role="alert" className="mb-3 rounded-xl bg-danger-pale px-4 py-2 text-sm text-danger">결제 목록을 갱신하지 못했어요 ({error}). {payments && "마지막으로 불러온 데이터를 보여주고 있어요."}</p>}

      <div className="grid gap-5 xl:grid-cols-[1fr_380px]">
        <div className="min-w-0 space-y-5">
          <section className="rounded-2xl bg-white shadow-card">
            <div className="flex items-center justify-between px-5 pt-4">
              <h2 className="text-base font-bold">결제</h2>
              {payments && <span className="text-xs text-muted">최신순 {payments.items.length} / 전체 {payments.total}건</span>}
            </div>
            {!payments ? (refreshing && <p role="status" className="p-5 text-sm text-muted">결제를 불러오는 중…</p>) : payments.items.length === 0 ? (
              <p className="p-5 text-sm text-muted">아직 결제가 없어요.</p>
            ) : (
              <div className="overflow-x-auto p-2">
                <table className="w-full border-collapse text-left text-sm">
                  <thead>
                    <tr className="text-xs text-muted">{headings.map((heading) => <th key={heading} scope="col" className="border-b border-[#E1E7E4] px-3 py-2 font-semibold">{heading}</th>)}</tr>
                  </thead>
                  <tbody>
                    {payments.items.map((payment) => (
                      <tr key={payment.payment_id} className={cx("hover:bg-canvas", selected?.payment_id === payment.payment_id && "bg-mint")}>
                        <th scope="row" className="border-b border-[#EEF1EF] px-3 py-2.5 font-normal">
                          <button type="button" className="text-left font-mono text-xs text-brand-strong underline-offset-2 hover:underline" aria-expanded={selected?.payment_id === payment.payment_id}
                            title={payment.payment_id}
                            onClick={() => setSelectedId(selectedId === payment.payment_id ? undefined : payment.payment_id)}>
                            <span className="block max-w-[7.5rem] truncate">{payment.payment_id}</span>
                          </button>
                          <span className="block text-[10px] text-muted">{formatTime(payment.created_at)}</span>
                        </th>
                        <td className="border-b border-[#EEF1EF] px-3 py-2.5 text-right font-semibold tabular">{payment.amount_krw != null ? amountFormat.format(payment.amount_krw) : "—"}</td>
                        <StatusCell value={payment.authorization_status} />
                        <StatusCell value={financialStatus(payment, payment.hold_status)} />
                        <StatusCell value={financialStatus(payment, payment.liquidation_status)} />
                        <StatusCell value={financialStatus(payment, payment.recovery_status)} />
                        <td className="border-b border-[#EEF1EF] px-3 py-2.5 font-mono text-xs" aria-label={failureInfo(payment)}>{failureInfo(payment)}</td>
                        <td className="border-b border-[#EEF1EF] px-3 py-2.5 text-xs text-muted" aria-label={payment.source}>{payment.source}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {overview && (
            <div className="grid gap-5 lg:grid-cols-[1.4fr_1fr]">
              <section className="rounded-2xl bg-white p-5 shadow-card">
                <h2 className="text-base font-bold">고객 지갑</h2>
                <ul className="mt-3 divide-y divide-[#EEF1EF]">
                  {overview.wallets.map((wallet) => (
                    <li key={wallet.wallet_id} className="flex items-center gap-3 py-2.5">
                      <span className={cx("h-2.5 w-2.5 rounded-full", wallet.locked ? "bg-warn" : "bg-brand")} aria-hidden />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-semibold">{wallet.name} {wallet.app_user && <Badge className="ml-1">앱 가입</Badge>}</p>
                        <p className="truncate text-[11px] text-muted">
                          {Object.entries(wallet.holdings_krw).filter(([, value]) => value > 0).map(([asset, value]) => `${asset} ${amountFormat.format(value)}`).join(" · ") || "잔고 없음"}
                          {" · "}카드 {wallet.cards}장
                        </p>
                      </div>
                      <div className="text-right">
                        <p className="text-sm font-bold tabular">{formatWon(wallet.total_krw)}</p>
                        {wallet.locked && <Badge tone="amber">LOCKED</Badge>}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
              <section className="rounded-2xl bg-white p-5 shadow-card">
                <h2 className="text-base font-bold">단말기</h2>
                <ul className="mt-3 space-y-2">
                  {overview.terminals.map((terminal) => (
                    <li key={terminal.terminal_id} className="flex items-center justify-between rounded-xl bg-canvas px-3 py-2.5 text-sm">
                      <span><span className="font-mono font-semibold">{terminal.terminal_id}</span> <span className="text-xs text-muted">· {terminal.merchant_id}</span></span>
                      {terminal.active_checkout ? <Badge tone="amber">{formatWon(terminal.active_amount_krw)} 대기</Badge> : <Badge tone="gray">대기 주문 없음</Badge>}
                    </li>
                  ))}
                </ul>
                <p className="mt-3 text-[11px] leading-relaxed text-muted">POS 화면에는 승인 결과만 보이고, 청산·회수 상태는 이 운영 화면에서만 봐요. 승인된 결제는 청산이 실패해도 FAILED로 바뀌지 않아요.</p>
              </section>
            </div>
          )}
        </div>
        <div>
          {selected ? <PaymentDetails key={selected.payment_id} payment={selected} refreshCount={refreshCount} onClose={() => setSelectedId(undefined)} />
            : <div className="rounded-2xl border-2 border-dashed border-[#DCE3DF] p-6 text-center text-sm text-muted">결제 ID를 누르면 스왑 내역과 이벤트 타임라인이 여기에 나와요.</div>}
        </div>
      </div>
    </main>
  );
}
