import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, ApiError, AssetIcon, Badge, Button, Card, cx, errorText, formatKrw, formatTime, formatWon, Notice, reasonText, Row, Spinner,
} from "../../ui/kit";
import { CardRegister } from "./CardRegister";
import { Onboarding } from "./Onboarding";
import { Screen } from "./Screen";
import {
  ASSET_NAMES, ASSETS, DEMO_PRICES, type ActivityItem, type Asset, type Me, type PaymentItem, type SellPreview,
} from "./types";

const STORAGE_KEY = "blockbucks.customer_id";
type Tab = "home" | "history" | "cards" | "profile";
type Sub = null | "deposit" | "withdraw" | "preview" | "wallet-ready" | "register";

function loadId() {
  try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
}
function saveId(id: string | null) {
  try { if (id) localStorage.setItem(STORAGE_KEY, id); else localStorage.removeItem(STORAGE_KEY); } catch { /* private mode */ }
}

export default function CustomerApp() {
  const [customerId, setCustomerId] = useState(loadId);
  const [me, setMe] = useState<Me | null>(null);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [tab, setTab] = useState<Tab>("home");
  const [sub, setSub] = useState<Sub>(null);
  const [loadError, setLoadError] = useState("");
  const [toast, setToast] = useState<PaymentItem | null>(null);
  const seenPayments = useRef<Map<string, string> | null>(null);

  const refresh = useCallback(async (id: string) => {
    const [profile, feed] = await Promise.all([
      api<Me>(`/api/app/users/${encodeURIComponent(id)}`),
      api<{ items: ActivityItem[] }>(`/api/app/users/${encodeURIComponent(id)}/activity`),
    ]);
    setMe(profile);
    setActivity(feed.items);
    setLoadError("");
    // Pop a banner when the store terminal approves or declines a new payment.
    const payments = feed.items.filter((item): item is PaymentItem => item.kind === "PAYMENT");
    const previous = seenPayments.current;
    const next = new Map(payments.map((item) => [item.id, item.status]));
    if (previous) {
      const fresh = payments.find((item) => item.status !== "AUTHORIZING" && previous.get(item.id) !== item.status);
      if (fresh) setToast(fresh);
    }
    seenPayments.current = next;
  }, []);

  useEffect(() => {
    if (!customerId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function loop() {
      try {
        await refresh(customerId!);
      } catch (failure) {
        if (!active) return;
        if (failure instanceof ApiError && failure.code === "UNKNOWN_CUSTOMER") {
          saveId(null);
          setCustomerId(null);
          setMe(null);
          return;
        }
        setLoadError(errorText(failure));
      }
      if (active) timer = setTimeout(loop, 2000);
    }
    void loop();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [customerId, refresh]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(timer);
  }, [toast]);

  const onRegistered = useCallback((fresh: Me) => {
    setMe(fresh);
    setSub(null);
    setTab("home");
  }, []);

  if (!customerId || !me) {
    if (customerId && !me) {
      return (
        <div className="flex min-h-dvh flex-col items-center justify-center gap-3 bg-canvas p-6 text-center">
          {loadError ? <Notice tone="red">{loadError}</Notice> : <Spinner className="h-6 w-6 text-brand" />}
          <button className="text-xs text-muted underline" onClick={() => { saveId(null); setCustomerId(null); }}>처음부터 다시 가입</button>
        </div>
      );
    }
    return <Onboarding onDone={(created) => {
      saveId(created.profile.customer_id);
      seenPayments.current = null;
      setMe(created);
      setCustomerId(created.profile.customer_id);
      setTab("home");
      setSub(created.cards.length ? null : "wallet-ready");
    }} />;
  }

  const close = () => setSub(null);
  const reload = () => void refresh(me.profile.customer_id).catch(() => undefined);

  let content;
  if (sub === "wallet-ready") content = <WalletReady me={me} onChanged={setMe} onNext={() => setSub("register")} />;
  else if (sub === "register") content = (
    <Screen title="카드 등록" onBack={close} footer={me.cards.length ? <Button block variant="secondary" onClick={close}>홈으로</Button> : undefined}>
      <CardRegister me={me} onRegistered={onRegistered} />
    </Screen>
  );
  else if (sub === "deposit") content = <Deposit me={me} onBack={close} onChanged={(fresh) => { setMe(fresh); reload(); }} />;
  else if (sub === "withdraw") content = <Withdraw me={me} onBack={close} onChanged={(fresh) => { setMe(fresh); reload(); }} />;
  else if (sub === "preview") content = <Preview me={me} onBack={close} />;
  else if (tab === "home") content = <Home me={me} activity={activity} open={setSub} openHistory={() => setTab("history")} />;
  else if (tab === "history") content = <History items={activity} />;
  else if (tab === "cards") content = <Cards me={me} onAdd={() => setSub("register")} onChanged={setMe} />;
  else content = <Profile me={me} onLogout={() => { saveId(null); setCustomerId(null); setMe(null); seenPayments.current = null; }} />;

  return (
    <div className="mx-auto min-h-dvh max-w-md bg-canvas">
      {toast && <PaymentToast item={toast} onClose={() => setToast(null)} />}
      {loadError && <div className="bg-danger px-4 py-2 text-center text-xs text-white" role="alert">{loadError}</div>}
      <div className={sub ? "" : "pb-20"}>{content}</div>
      {!sub && (
        <nav aria-label="하단 메뉴" className="fixed inset-x-0 bottom-0 z-20 mx-auto grid max-w-md grid-cols-4 border-t border-[#E1E7E4] bg-white pb-[env(safe-area-inset-bottom)]">
          {([["home", "홈", "⌂"], ["history", "결제내역", "▤"], ["cards", "카드", "▭"], ["profile", "내 정보", "◉"]] as const).map(([id, label, icon]) => (
            <button key={id} type="button" onClick={() => setTab(id)} aria-current={tab === id ? "page" : undefined}
              className={cx("flex flex-col items-center gap-0.5 py-2.5 text-[11px]", tab === id ? "font-bold text-brand-deep" : "text-muted")}>
              <span className="text-lg leading-none" aria-hidden>{icon}</span>{label}
            </button>
          ))}
        </nav>
      )}
    </div>
  );
}

function PaymentToast({ item, onClose }: { item: PaymentItem; onClose: () => void }) {
  const ok = item.authorization_status === "AUTHORIZED";
  return (
    <button type="button" onClick={onClose} role="status"
      className={cx("fixed inset-x-3 top-3 z-30 mx-auto flex max-w-md animate-pop items-center gap-3 rounded-2xl px-4 py-3.5 text-left text-white shadow-lg",
        ok ? "bg-brand-deep" : "bg-danger")}>
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white/20 text-lg">{ok ? "✓" : "!"}</span>
      <span className="min-w-0 flex-1">
        <span className="block text-[15px] font-bold">{ok ? `${formatWon(item.amount_krw)} 결제 완료` : "결제가 거절됐어요"}</span>
        <span className="block truncate text-xs text-white/80">{item.merchant} · {ok ? "코인은 잠시 후 자동 매도돼요" : reasonText[item.reason_code ?? ""] ?? item.reason_code}</span>
      </span>
    </button>
  );
}

function LockBanner({ me }: { me: Me }) {
  if (!me.wallet.locked) return null;
  return (
    <Notice tone="amber" title={<span className="flex items-center gap-2"><Spinner className="h-3.5 w-3.5" />결제 정산 중 · 지갑 잠금</span>}>
      {me.wallet.lock_amount_krw ? `${formatWon(me.wallet.lock_amount_krw)} 결제의 ` : ""}코인을 매도하고 있어요. 끝나면 자동으로 풀리고, 그동안은 출금과 다른 결제가 잠시 멈춰요.
    </Notice>
  );
}

function Home({ me, activity, open, openHistory }: { me: Me; activity: ActivityItem[]; open: (sub: Sub) => void; openHistory: () => void }) {
  const wallet = me.wallet;
  const external = wallet.mode === "external";
  return (
    <div className="px-4 pt-4">
      <header className="mb-4 flex items-center justify-between">
        <p className="text-xl font-extrabold tracking-tight text-brand-deep">BlockBucks</p>
        <span className="text-sm text-muted">{me.profile.name}님</span>
      </header>
      <Card className="bg-brand-deep text-white">
        <div className="flex items-start justify-between">
          <div>
            <p className="text-xs text-white/70">총 결제 가능 자산</p>
            <p className="mt-1 text-3xl font-extrabold tabular">{external ? "엔진 관리" : formatWon(wallet.total_krw)}</p>
          </div>
          <Badge tone={wallet.locked ? "amber" : "green"}>{wallet.locked ? "🔒 잠김" : "결제 가능"}</Badge>
        </div>
        {!external && <p className="mt-2 text-xs text-white/60">오늘 매도 가능 {formatWon(wallet.sellable_today_krw)} · {wallet.prices_note}</p>}
        <div className="mt-4 grid grid-cols-3 gap-2">
          <Button variant="secondary" className="bg-white/15 text-white hover:bg-white/25" onClick={() => open("deposit")} disabled={external}>충전</Button>
          <Button variant="secondary" className="bg-white/15 text-white hover:bg-white/25" onClick={() => open("withdraw")} disabled={external}>출금</Button>
          <Button variant="secondary" className="bg-white/15 text-white hover:bg-white/25" onClick={() => open("preview")} disabled={external}>미리보기</Button>
        </div>
      </Card>
      <div className="mt-3"><LockBanner me={me} /></div>
      {me.cards.length === 0 && (
        <button type="button" onClick={() => open("register")} className="mt-3 flex w-full items-center justify-between rounded-2xl border-2 border-dashed border-mint-line bg-mint px-4 py-3.5 text-left">
          <span><span className="block text-sm font-bold text-brand-deep">카드를 등록해야 결제할 수 있어요</span><span className="text-xs text-muted">매장 단말기에 카드를 한 번 대면 끝나요</span></span>
          <span className="text-xl text-brand">›</span>
        </button>
      )}
      {external ? (
        <Card className="mt-3"><Notice>외부 결제 엔진 모드예요. 지갑 잔고와 충전·출금은 Stockbucks 엔진에서 관리하고, 이 앱은 카드 등록과 결제 내역을 보여줘요.</Notice></Card>
      ) : (
        <Card className="mt-3">
          <div className="mb-2 flex items-center justify-between"><p className="text-sm font-bold">내 코인</p><span className="text-[11px] text-muted">원화 환산</span></div>
          <ul className="divide-y divide-[#EEF1EF]">
            {wallet.assets.map((item) => (
              <li key={item.asset} className="flex items-center gap-3 py-2.5">
                <AssetIcon asset={item.asset} />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold">{item.asset} <span className="text-xs font-normal text-muted">{ASSET_NAMES[item.asset]}</span></p>
                  <p className="text-xs text-muted tabular">{item.quantity} {item.asset}</p>
                </div>
                <div className="text-right">
                  <p className="text-sm font-bold tabular">{formatWon(item.value_krw)}</p>
                  {!item.stablecoin && item.value_krw > 0 && <p className="text-[11px] text-muted">오늘 {formatWon(item.sellable_today_krw)} 매도 가능</p>}
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card className="mt-3">
        <div className="mb-1 flex items-center justify-between">
          <p className="text-sm font-bold">최근 내역</p>
          <button type="button" onClick={openHistory} className="text-xs font-semibold text-mint-text">전체 보기</button>
        </div>
        {activity.length === 0 ? <p className="py-4 text-center text-xs text-muted">아직 내역이 없어요</p>
          : <ul className="divide-y divide-[#EEF1EF]">{activity.slice(0, 4).map((item) => <ActivityRow key={item.id} item={item} />)}</ul>}
      </Card>
    </div>
  );
}

function paymentBadge(item: PaymentItem) {
  if (item.status === "AUTHORIZING") return <Badge tone="amber">승인 확인 중</Badge>;
  if (item.authorization_status !== "AUTHORIZED") return <Badge tone="red">거절 · {reasonText[item.reason_code ?? ""] ?? item.reason_code}</Badge>;
  if (item.financial_status_reported === false) return <Badge>승인</Badge>;
  if (item.settlement_status === "COMPLETED") return <Badge>정산 완료</Badge>;
  if (item.recovery_status === "LOSS_ABSORBED") return <Badge tone="gray">승인 · 회사 부담 처리</Badge>;
  return <Badge tone="amber">승인 · 코인 매도 중</Badge>;
}

function ActivityRow({ item, expanded, onToggle }: { item: ActivityItem; expanded?: boolean; onToggle?: () => void }) {
  if (item.kind !== "PAYMENT") {
    const deposit = item.kind === "DEPOSIT";
    return (
      <li className="flex items-center gap-3 py-3">
        <AssetIcon asset={item.asset} size={32} />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">{deposit ? "충전" : "출금"} · {item.asset}</p>
          <p className="text-xs text-muted">{formatTime(item.created_at)}{item.address ? ` · ${item.address.slice(0, 10)}…` : ""}</p>
        </div>
        <div className="text-right">
          <p className={cx("text-sm font-bold tabular", deposit ? "text-mint-text" : "text-brand-ink")}>{deposit ? "+" : "−"}{item.quantity} {item.asset}</p>
          <p className="text-[11px] text-muted">{formatWon(item.amount_krw)}</p>
        </div>
      </li>
    );
  }
  const declined = item.authorization_status === "DECLINED";
  return (
    <li className="py-3">
      <button type="button" className="flex w-full items-center gap-3 text-left" onClick={onToggle} aria-expanded={expanded} disabled={!onToggle}>
        <span className={cx("flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-bold", declined ? "bg-danger-pale text-danger" : "bg-mint text-brand")}>₩</span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold">{item.merchant}</p>
          <p className="text-xs text-muted">{formatTime(item.created_at)}</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <p className={cx("text-sm font-bold tabular", declined && "text-muted line-through")}>−{formatWon(item.amount_krw)}</p>
          {paymentBadge(item)}
        </div>
      </button>
      {expanded && !declined && (
        <div className="mt-3 space-y-1.5 rounded-xl bg-canvas p-3 text-[13px]">
          <p className="text-xs font-semibold text-muted">차감된 코인</p>
          {item.sold.length === 0
            ? <p className="text-xs text-muted">{item.settlement_status === "COMPLETED" || item.recovery_status === "LOSS_ABSORBED" ? "매도된 코인이 없어요" : "매도가 아직 진행 중이에요"}</p>
            : item.sold.map((leg, index) => (
              <div key={index} className="flex items-center justify-between">
                <span className="flex items-center gap-2"><AssetIcon asset={leg.asset} size={20} />{leg.asset}</span>
                <span className="tabular">−{leg.quantity} {leg.asset} <span className="text-muted">({formatWon(leg.value_krw)})</span></span>
              </div>
            ))}
          {item.excess_returned_krw > 0 && (
            <div className="flex items-center justify-between border-t border-[#E1E7E4] pt-1.5">
              <span className="text-muted">여유분 환급 (USDC)</span>
              <span className="tabular text-mint-text">+{item.excess_returned_usdc} USDC ({formatWon(item.excess_returned_krw)})</span>
            </div>
          )}
          {item.recovery_status === "LOSS_ABSORBED" && <p className="text-xs text-muted">매도가 끝나지 않은 부분은 회사가 부담했어요. 추가 청구는 없어요.</p>}
        </div>
      )}
    </li>
  );
}

function History({ items }: { items: ActivityItem[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "pay" | "wallet">("all");
  const shown = items.filter((item) => filter === "all" || (filter === "pay") === (item.kind === "PAYMENT"));
  const paid = items.filter((item): item is PaymentItem => item.kind === "PAYMENT" && item.authorization_status === "AUTHORIZED");
  return (
    <Screen title="결제 · 차감 내역">
      <Card>
        <div className="grid grid-cols-2 gap-2 text-center">
          <div className="rounded-xl bg-canvas py-3"><p className="text-xs text-muted">승인된 결제</p><p className="mt-0.5 text-lg font-bold tabular">{formatWon(paid.reduce((sum, item) => sum + item.amount_krw, 0))}</p></div>
          <div className="rounded-xl bg-canvas py-3"><p className="text-xs text-muted">차감된 코인 가치</p><p className="mt-0.5 text-lg font-bold tabular">{formatWon(paid.reduce((sum, item) => sum + item.sold_krw - item.excess_returned_krw, 0))}</p></div>
        </div>
      </Card>
      <div className="my-3 flex gap-2">
        {([["all", "전체"], ["pay", "결제"], ["wallet", "충전·출금"]] as const).map(([id, label]) => (
          <button key={id} type="button" onClick={() => setFilter(id)} aria-pressed={filter === id}
            className={cx("rounded-full px-3.5 py-1.5 text-xs font-semibold", filter === id ? "bg-brand-deep text-white" : "bg-white text-muted")}>{label}</button>
        ))}
      </div>
      <Card>
        {shown.length === 0 ? <p className="py-6 text-center text-xs text-muted">내역이 없어요</p> : (
          <ul className="divide-y divide-[#EEF1EF]">
            {shown.map((item) => <ActivityRow key={item.id} item={item} expanded={open === item.id}
              onToggle={item.kind === "PAYMENT" ? () => setOpen(open === item.id ? null : item.id) : undefined} />)}
          </ul>
        )}
      </Card>
      <p className="mt-3 text-center text-[11px] text-muted">결제를 누르면 어떤 코인이 얼마나 팔렸는지 볼 수 있어요.</p>
    </Screen>
  );
}

function AmountForm({ me, mode, onSubmit, busy }: {
  me: Me; mode: "deposit" | "withdraw"; busy: boolean; onSubmit: (asset: Asset, amountKrw: number, address?: string) => void;
}) {
  const [asset, setAsset] = useState<Asset>(me.profile.pay_assets[0] ?? "USDC");
  const [unit, setUnit] = useState<"coin" | "krw">("coin");
  const [value, setValue] = useState(asset === "USDC" || asset === "USDT" ? "100" : "");
  const [address, setAddress] = useState("");
  const amountKrw = useMemo(() => {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return 0;
    return Math.round(unit === "coin" ? number * DEMO_PRICES[asset] : number);
  }, [asset, unit, value]);
  const held = me.wallet.assets.find((item) => item.asset === asset);
  const over = mode === "withdraw" && held !== undefined && amountKrw > held.value_krw;
  return (
    <Card>
      <p className="text-sm font-bold">코인</p>
      <div className="mt-2 grid grid-cols-4 gap-2">
        {ASSETS.map((item) => (
          <button key={item} type="button" aria-pressed={asset === item} onClick={() => setAsset(item)}
            className={cx("flex flex-col items-center gap-1 rounded-xl py-2.5 text-xs font-semibold", asset === item ? "bg-mint ring-2 ring-brand" : "bg-canvas text-muted")}>
            <AssetIcon asset={item} size={24} />{item}
          </button>
        ))}
      </div>
      <div className="mt-4 flex items-center justify-between">
        <label className="text-sm font-bold" htmlFor={`${mode}-amount`}>수량</label>
        <div className="flex rounded-lg bg-canvas p-0.5 text-xs font-semibold">
          {(["coin", "krw"] as const).map((item) => (
            <button key={item} type="button" onClick={() => setUnit(item)} className={cx("rounded-md px-2.5 py-1", unit === item ? "bg-white shadow-sm" : "text-muted")}>{item === "coin" ? asset : "원화"}</button>
          ))}
        </div>
      </div>
      <div className="mt-2 flex items-center rounded-xl bg-canvas px-3.5">
        <input id={`${mode}-amount`} inputMode="decimal" value={value} onChange={(event) => setValue(event.target.value.replace(/[^0-9.]/g, ""))}
          className="w-full bg-transparent py-3 text-lg font-bold tabular outline-none" placeholder="0" />
        <span className="text-sm text-muted">{unit === "coin" ? asset : "원"}</span>
      </div>
      <p className="mt-1.5 text-xs text-muted">≈ {formatWon(amountKrw)}{held ? ` · 보유 ${held.quantity} ${asset} (${formatWon(held.value_krw)})` : ""}</p>
      {mode === "withdraw" && (
        <>
          <label className="mt-4 block text-sm font-bold" htmlFor="withdraw-address">받을 주소 (Base)</label>
          <input id="withdraw-address" value={address} onChange={(event) => setAddress(event.target.value)} placeholder="0x…"
            className="mt-2 w-full rounded-xl bg-canvas px-3.5 py-3 font-mono text-sm" spellCheck={false} />
        </>
      )}
      {over && <p className="mt-2 text-xs text-danger">보유량보다 많아요.</p>}
      <Button block className="mt-4" disabled={busy || amountKrw < 1 || amountKrw > 10_000_000 || over || (mode === "withdraw" && !/^0x[0-9a-fA-F]{6,}$/.test(address))}
        onClick={() => onSubmit(asset, amountKrw, address || undefined)}>
        {busy ? <Spinner /> : null}{mode === "deposit" ? `${formatWon(amountKrw)} 충전하기` : `${formatWon(amountKrw)} 출금하기`}
      </Button>
    </Card>
  );
}

function PseudoQr({ seed }: { seed: string }) {
  // Decorative stand-in for a deposit QR in simulation mode.
  const cells = Array.from({ length: 21 * 21 }, (_, index) => {
    const x = index % 21, y = Math.floor(index / 21);
    const finder = (cx: number, cy: number) => x >= cx && x < cx + 7 && y >= cy && y < cy + 7
      && (x === cx || x === cx + 6 || y === cy || y === cy + 6 || (x >= cx + 2 && x <= cx + 4 && y >= cy + 2 && y <= cy + 4));
    if (finder(0, 0) || finder(14, 0) || finder(0, 14)) return true;
    if ((x < 8 && y < 8) || (x > 12 && y < 8) || (x < 8 && y > 12)) return false;
    return seed.charCodeAt((x * 7 + y * 13) % seed.length) % 3 === 0;
  });
  return (
    <svg viewBox="0 0 21 21" className="h-36 w-36" shapeRendering="crispEdges" aria-label="입금 주소 QR (시뮬레이션)">
      <rect width="21" height="21" fill="white" />
      {cells.map((on, index) => on && <rect key={index} x={index % 21} y={Math.floor(index / 21)} width="1" height="1" fill="#14251F" />)}
    </svg>
  );
}

function Deposit({ me, onBack, onChanged }: { me: Me; onBack: () => void; onChanged: (me: Me) => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "green" | "red"; text: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const address = me.wallet.address ?? "";
  async function submit(asset: Asset, amountKrw: number) {
    setBusy(true);
    setMessage(null);
    try {
      const fresh = await api<Me>(`/api/app/users/${encodeURIComponent(me.profile.customer_id)}/deposits`, {
        method: "POST", json: { asset, amount_krw: amountKrw }, idempotencyKey: crypto.randomUUID(),
      });
      onChanged(fresh);
      setMessage({ tone: "green", text: `${asset} ${formatWon(amountKrw)}어치가 지갑에 들어왔어요.` });
    } catch (failure) {
      setMessage({ tone: "red", text: errorText(failure) });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Screen title="코인 충전" onBack={onBack} action={<Badge>Base</Badge>}>
      <div className="flex items-center justify-between rounded-2xl bg-brand-deep px-4 py-3 text-white">
        <div><p className="text-sm font-bold">BASE 네트워크 전용</p><p className="text-[11px] text-white/70">Chain ID 8453 · Coinbase L2</p></div>
        <Badge>필수</Badge>
      </div>
      <Card className="mt-3 flex flex-col items-center">
        <p className="text-sm font-bold">QR을 스캔해 Base로 입금</p>
        <div className="my-3 rounded-xl border border-[#E1E7E4] p-2"><PseudoQr seed={address || "blockbucks"} /></div>
        <div className="flex w-full items-center gap-2 rounded-xl bg-canvas px-3 py-2.5">
          <span className="flex-1 truncate font-mono text-xs">{address}</span>
          <button type="button" className="rounded-lg bg-white px-2.5 py-1 text-xs font-semibold shadow-sm"
            onClick={() => { void navigator.clipboard?.writeText(address).then(() => setCopied(true)); }}>{copied ? "복사됨" : "복사"}</button>
        </div>
        <p className="mt-2 text-[11px] text-muted">데모 시뮬레이션 주소예요. 실제 코인을 보내지 마세요.</p>
      </Card>
      <div className="mt-3">
        <Notice tone="red" title="다른 네트워크로 보내지 마세요">Base 네트워크로만 입금하세요. 이더리움 메인넷(ERC-20), Arbitrum, Polygon, Solana로 보내면 영구히 잃을 수 있어요.</Notice>
      </div>
      <p className="mb-2 mt-5 px-1 text-sm font-bold">데모 입금 시뮬레이션</p>
      <AmountForm me={me} mode="deposit" busy={busy} onSubmit={(asset, amount) => void submit(asset, amount)} />
      {message && <div className="mt-3"><Notice tone={message.tone}>{message.text}</Notice></div>}
    </Screen>
  );
}

function Withdraw({ me, onBack, onChanged }: { me: Me; onBack: () => void; onChanged: (me: Me) => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "green" | "red"; title?: string; text: string } | null>(null);
  async function submit(asset: Asset, amountKrw: number, address?: string) {
    setBusy(true);
    setMessage(null);
    try {
      const fresh = await api<Me>(`/api/app/users/${encodeURIComponent(me.profile.customer_id)}/withdrawals`, {
        method: "POST", json: { asset, amount_krw: amountKrw, address }, idempotencyKey: crypto.randomUUID(),
      });
      onChanged(fresh);
      setMessage({ tone: "green", text: `${asset} ${formatWon(amountKrw)}어치 출금을 요청했어요.` });
    } catch (failure) {
      const locked = failure instanceof ApiError && failure.code === "WALLET_LOCKED";
      setMessage({ tone: "red", title: locked ? "출금 일시 중지 · 지갑 잠김 (HTTP 409)" : undefined, text: errorText(failure) });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Screen title="지갑 · 출금" onBack={onBack} action={me.wallet.locked ? <Badge tone="amber">잠김</Badge> : <Badge>사용 가능</Badge>}>
      <Card>
        <p className="text-xs text-muted">지갑 총액</p>
        <p className="mt-0.5 text-2xl font-extrabold tabular">{formatWon(me.wallet.total_krw)}</p>
        <div className="mt-3 space-y-1.5">
          <Row label="결제 정산 잠금" value={me.wallet.locked ? <span className="text-danger">{formatWon(me.wallet.lock_amount_krw)} 결제 정산 중</span> : "없음"} />
          <Row label="오늘 매도 가능 (일일 한도)" value={formatWon(me.wallet.sellable_today_krw)} />
        </div>
      </Card>
      <div className="mt-3"><LockBanner me={me} /></div>
      <p className="mb-2 mt-4 px-1 text-sm font-bold">코인 출금 (Base)</p>
      <AmountForm me={me} mode="withdraw" busy={busy} onSubmit={(asset, amount, address) => void submit(asset, amount, address)} />
      {message && <div className="mt-3"><Notice tone={message.tone} title={message.title}>{message.text}</Notice></div>}
    </Screen>
  );
}

function Preview({ me, onBack }: { me: Me; onBack: () => void }) {
  const [amount, setAmount] = useState("4500");
  const [preview, setPreview] = useState<SellPreview | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const value = Number(amount);
    if (!Number.isInteger(value) || value < 1 || value > 10_000_000) { setPreview(null); return; }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api<SellPreview>(`/api/app/users/${encodeURIComponent(me.profile.customer_id)}/sell-preview?amount_krw=${value}`, { signal: controller.signal })
        .then((result) => { setPreview(result); setError(""); })
        .catch((failure) => { if (!controller.signal.aborted) setError(errorText(failure)); });
    }, 250);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [amount, me.profile.customer_id, me.wallet.total_krw]);
  return (
    <Screen title="매도 미리보기" onBack={onBack} footer={<Button block variant="secondary" onClick={onBack}>홈으로</Button>}>
      <Card>
        <label className="text-sm font-bold" htmlFor="preview-amount">결제 금액</label>
        <div className="mt-2 flex items-center rounded-xl bg-mint px-3.5">
          <input id="preview-amount" inputMode="numeric" value={amount} onChange={(event) => setAmount(event.target.value.replace(/\D/g, ""))}
            className="w-full bg-transparent py-3 text-right text-lg font-bold tabular outline-none" />
          <span className="ml-1 text-sm text-muted">원</span>
        </div>
      </Card>
      <Card className="mt-3">
        <p className="text-sm font-bold">팔리게 될 코인 (예상)</p>
        {preview ? (
          <>
            <p className="mt-0.5 text-xs text-muted">지금 {formatWon(preview.amount_krw)}을 결제하면 여유분 {Number(preview.margin) * 100}% 포함 {formatWon(preview.required_krw)}어치가 팔려요.</p>
            {preview.status === "LIMIT_EXCEEDED" ? (
              <div className="mt-3"><Notice tone="red" title="한도 초과">지갑 총액 {formatWon(preview.total_krw)}이 필요액보다 적어서 결제가 거절돼요.</Notice></div>
            ) : (
              <div className="mt-3 space-y-1.5">
                {preview.legs.map((leg, index) => (
                  <Row key={leg.asset} label={<span className="flex items-center gap-2"><AssetIcon asset={leg.asset} size={20} />{index + 1}. {leg.asset}</span>}
                    value={`${leg.quantity} ${leg.asset} (≈ ${formatWon(leg.value_krw)})`} />
                ))}
                {preview.status === "EXECUTION_LIMITED" && <Notice tone="amber">오늘 코인별 매도 한도(30%) 때문에 일부만 팔 수 있어요. 결제가 거절될 수 있어요.</Notice>}
              </div>
            )}
            <p className="mt-3 rounded-xl bg-mint px-3 py-2 text-[11px] font-semibold text-mint-text">예상치일 뿐이에요. 실제로 코인이 팔리지는 않아요.</p>
          </>
        ) : <p className="mt-2 text-xs text-muted">{error || "1원 이상 금액을 입력해 주세요."}</p>}
      </Card>
    </Screen>
  );
}

function WalletReady({ me, onChanged, onNext }: { me: Me; onChanged: (me: Me) => void; onNext: () => void }) {
  const [funding, setFunding] = useState(false);
  const [funded, setFunded] = useState(false);
  const [error, setError] = useState("");
  async function fund() {
    setFunding(true);
    try {
      onChanged(await api<Me>(`/api/app/users/${encodeURIComponent(me.profile.customer_id)}/deposits`, {
        method: "POST", json: { asset: "USDC", amount_krw: 100 * DEMO_PRICES.USDC }, idempotencyKey: crypto.randomUUID(),
      }));
      setFunded(true);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setFunding(false);
    }
  }
  const external = me.wallet.mode === "external";
  return (
    <Screen title="지갑 생성 완료" step="✓" footer={<Button block onClick={onNext}>다음: NFC 카드 등록</Button>}>
      <Card>
        <p className="text-[15px] font-bold">{me.profile.name}님의 BlockBucks 지갑이 준비됐어요</p>
        <div className="mt-3 space-y-1.5">
          <Row label="지갑 ID" value={<span className="font-mono text-xs">{me.wallet.wallet_id ?? "엔진에서 발급"}</span>} />
          {me.wallet.address && <Row label="지갑 주소 (sim)" value={<span className="font-mono text-xs">{me.wallet.address.slice(0, 8)}…{me.wallet.address.slice(-6)}</span>} />}
        </div>
      </Card>
      {!external && (
        <Card className="mt-3 bg-brand-deep text-white">
          <div className="flex items-center justify-between">
            <p className="text-[15px] font-bold">+100 USDC 데모 충전</p>
            {funded ? <Badge>완료</Badge> : <Badge tone="dark" className="bg-white/15">시뮬레이션</Badge>}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-white/75">바이낸스에서 100 USDC를 지갑으로 보냈다고 가정해요. 나중에 홈의 ‘충전’에서 다른 코인도 넣을 수 있어요.</p>
          {!funded && <Button variant="secondary" className="mt-3 bg-white text-brand-deep" disabled={funding} onClick={() => void fund()}>{funding ? <Spinner /> : null}100 USDC 넣기</Button>}
        </Card>
      )}
      {error && <div className="mt-3"><Notice tone="red">{error}</Notice></div>}
    </Screen>
  );
}

function Cards({ me, onAdd, onChanged }: { me: Me; onAdd: () => void; onChanged: (me: Me) => void }) {
  const [error, setError] = useState("");
  async function remove(alias: string) {
    try {
      onChanged(await api<Me>(`/api/app/users/${encodeURIComponent(me.profile.customer_id)}/cards/${encodeURIComponent(alias)}`, { method: "DELETE" }));
    } catch (failure) {
      setError(errorText(failure));
    }
  }
  return (
    <Screen title="내 카드" footer={<Button block onClick={onAdd}>+ 카드 등록</Button>}>
      {me.cards.length === 0 ? (
        <Card><p className="py-8 text-center text-sm text-muted">등록된 카드가 없어요</p></Card>
      ) : me.cards.map((card) => (
        <div key={card.card_alias} className="mb-3 overflow-hidden rounded-2xl bg-gradient-to-br from-brand to-brand-deep p-5 text-white shadow-card">
          <div className="flex items-start justify-between">
            <p className="text-lg font-extrabold">BlockBucks</p>
            <Badge tone="dark" className="bg-white/15">{card.kind === "nfc" ? "NFC" : "데모"}</Badge>
          </div>
          <p className="mt-8 font-mono text-lg tracking-widest">{card.card_alias.replace(/(.{2})(?=.)/g, "$1 ")}</p>
          <div className="mt-3 flex items-end justify-between text-xs text-white/70">
            <span>{me.profile.name}{card.card_token ? ` · token ${card.card_token.slice(0, 6)}…${card.card_token.slice(-6)}` : ""}</span>
            <button type="button" className="rounded-lg bg-white/15 px-2.5 py-1 text-white hover:bg-white/25" onClick={() => void remove(card.card_alias)}>해지</button>
          </div>
        </div>
      ))}
      {me.profile.engine_mode === "external" && me.cards.length > 0 && (
        <Notice tone="amber" title="엔진 등록 필요">외부 엔진 모드에서는 위 카드 토큰을 엔진 담당자가 POST /v1/cards로 지갑에 등록해야 결제돼요.</Notice>
      )}
      {error && <div className="mt-3"><Notice tone="red">{error}</Notice></div>}
    </Screen>
  );
}

function Profile({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const risk = { conservative: "보수적", neutral: "중립", aggressive: "공격적" }[me.profile.risk_profile] ?? me.profile.risk_profile;
  return (
    <Screen title="내 정보">
      <Card>
        <div className="space-y-1.5">
          <Row label="이름" value={me.profile.name} />
          <Row label="결제 코인" value={me.profile.pay_assets.join(", ")} />
          <Row label="매도 성향" value={risk} />
          <Row label="고객 ID" value={<span className="font-mono text-xs">{me.profile.customer_id}</span>} />
          <Row label="결제 엔진" value={me.profile.engine_mode === "external" ? "Stockbucks 엔진" : "데모 시뮬레이션"} />
        </div>
      </Card>
      <div className="mt-3 space-y-2">
        <Notice>결제 승인 후 엔진이 코인을 매도하는 동안 지갑이 몇 초간 잠겨요. 결제 가능액은 지갑 총액 ÷ 1.03 정도예요.</Notice>
      </div>
      <Button variant="ghost" block className="mt-4 text-danger" onClick={onLogout}>이 기기에서 로그아웃</Button>
    </Screen>
  );
}
