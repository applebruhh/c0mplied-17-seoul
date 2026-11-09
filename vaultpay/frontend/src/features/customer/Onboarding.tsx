import { useEffect, useState } from "react";
import { api, Badge, Button, Card, cx, errorText, Notice, Row } from "../../ui/kit";
import { ASSET_NAMES, ASSETS, type Asset, type Me } from "./types";
import { Screen } from "./Screen";

type Step = "splash" | "intro" | "info" | "consent";
const slides = [
  ["즉시 결제 승인", "매장에서 카드를 대면 원화 결제가 바로 승인돼요."],
  ["코인 자동 매도", "승인 뒤에 엔진이 지갑 속 코인을 필요한 만큼만 자동으로 팔아요."],
  ["몇 초간 지갑 잠금", "정산되는 동안 잔고가 꼬이지 않도록 지갑이 잠깐 잠겨요."],
] as const;
const risks = [
  { id: "conservative", title: "보수적 · 손실 회피 우선", detail: "스테이블코인(USDC)을 먼저 써서 손실 보고 파는 일을 줄여요." },
  { id: "neutral", title: "중립 · 균형 보호", detail: "손실 회피와 적당한 수익 실현 사이에서 균형을 잡아요." },
  { id: "aggressive", title: "공격적 · 수익 난 코인부터", detail: "오를 코인은 남기고 수익이 난 코인부터 써요." },
] as const;

export function Onboarding({ onDone, onBack }: { onDone: (me: Me) => void; onBack?: () => void }) {
  const [step, setStep] = useState<Step>("splash");
  const [name, setName] = useState("");
  const [assets, setAssets] = useState<Asset[]>(["USDC", "ETH", "BTC"]);
  const [risk, setRisk] = useState<string>("neutral");
  const [delegation, setDelegation] = useState(false);
  const [lockNotice, setLockNotice] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [accounts, setAccounts] = useState<Array<{ customer_id: string; name: string; cards: string[] }>>([]);

  useEffect(() => {
    api<{ items: typeof accounts }>("/api/app/demo-accounts").then((result) => setAccounts(result.items)).catch(() => undefined);
  }, []);

  async function connect(customerId: string) {
    setBusy(true);
    setError("");
    try {
      onDone(await api<Me>(`/api/app/users/${encodeURIComponent(customerId)}`));
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    setBusy(true);
    setError("");
    try {
      onDone(await api<Me>("/api/app/users", {
        method: "POST",
        json: { name: name.trim(), pay_assets: assets, risk_profile: risk, delegation_agreed: delegation, lock_notice_agreed: lockNotice },
      }));
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  if (step === "splash") {
    return (
      <div className="flex min-h-dvh flex-col bg-brand px-6 pb-8 pt-16 text-white">
        <div className="flex flex-1 flex-col items-center justify-center text-center">
          <div className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-white/15 text-3xl font-black">B</div>
          <h1 className="text-4xl font-extrabold tracking-tight">BlockBucks</h1>
          <p className="mt-3 text-[15px] text-white/85">카드 한 장으로, 내 코인으로 결제해요.</p>
        </div>
        <Button variant="secondary" block className="bg-white py-4 text-base text-brand-deep" onClick={() => setStep("intro")}>시작하기</Button>
        {accounts.length > 0 && (
          <details className="mt-4 rounded-xl bg-white/10 px-4 py-3 text-sm">
            <summary className="cursor-pointer font-medium">이미 카드가 등록된 계정으로 연결</summary>
            <ul className="mt-3 space-y-2">
              {accounts.map((account) => (
                <li key={account.customer_id}>
                  <button type="button" disabled={busy} onClick={() => void connect(account.customer_id)}
                    className="flex w-full items-center justify-between rounded-lg bg-white/15 px-3 py-2.5 text-left hover:bg-white/25">
                    <span className="font-semibold">{account.name}</span>
                    <span className="text-xs text-white/80">{account.cards.join(", ") || "데모 카드"}</span>
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
        {error && <p className="mt-3 text-center text-sm text-white" role="alert">{error}</p>}
      </div>
    );
  }

  if (step === "intro") {
    return (
      <Screen title="이렇게 결제돼요" onBack={() => setStep("splash")} action={<button className="text-xs font-semibold text-mint-text" onClick={() => setStep("info")}>건너뛰기</button>}
        footer={<Button block onClick={() => setStep("info")}>가입 계속하기</Button>}>
        <div className="space-y-3">
          {slides.map(([title, detail], index) => (
            <Card key={title} className={cx(index === 2 && "border-2 border-mint-line")}>
              <p className="text-[15px] font-bold text-brand-strong">{index + 1}. {title}</p>
              <p className="mt-1 text-[13px] leading-relaxed text-muted">{detail}</p>
            </Card>
          ))}
        </div>
      </Screen>
    );
  }

  if (step === "info") {
    return (
      <Screen title="가입 정보" onBack={() => setStep("intro")} step="1/2"
        footer={<Button block disabled={!name.trim() || assets.length === 0} onClick={() => setStep("consent")}>다음: 필수 동의</Button>}>
        <Card>
          <label className="block text-sm font-bold" htmlFor="signup-name">1. 이름</label>
          <input id="signup-name" value={name} maxLength={40} onChange={(event) => setName(event.target.value)} placeholder="홍길동"
            className="mt-2 w-full rounded-xl bg-canvas px-3.5 py-3 text-[15px] outline-none ring-brand focus:ring-2" autoComplete="name" />
          <p className="mt-5 text-sm font-bold">2. 결제에 쓸 코인</p>
          <div className="mt-2 grid grid-cols-4 gap-2">
            {ASSETS.map((asset) => {
              const on = assets.includes(asset);
              return (
                <button key={asset} type="button" aria-pressed={on}
                  onClick={() => setAssets(on ? assets.filter((item) => item !== asset) : [...assets, asset])}
                  className={cx("rounded-xl py-2.5 text-[13px] font-semibold", on ? "bg-brand text-white" : "bg-canvas text-muted")}>
                  {asset}{on && " ✓"}
                </button>
              );
            })}
          </div>
          <p className="mt-1.5 text-xs text-muted">{assets.map((asset) => ASSET_NAMES[asset]).join(" · ") || "하나 이상 골라 주세요"}</p>
        </Card>
        <Card className="mt-3">
          <p className="text-sm font-bold">3. 매도 성향</p>
          <p className="mt-0.5 text-xs text-muted">코인을 팔 때 어떤 손실을 가장 피하고 싶나요?</p>
          <div className="mt-3 space-y-2" role="radiogroup">
            {risks.map((item) => (
              <button key={item.id} type="button" role="radio" aria-checked={risk === item.id} onClick={() => setRisk(item.id)}
                className={cx("w-full rounded-xl border px-3.5 py-3 text-left", risk === item.id ? "border-brand bg-mint" : "border-transparent bg-canvas")}>
                <span className="block text-[13px] font-bold">{item.title}{risk === item.id && " ✓"}</span>
                <span className="mt-0.5 block text-xs text-muted">{item.detail}</span>
              </button>
            ))}
          </div>
        </Card>
      </Screen>
    );
  }

  const checkbox = (checked: boolean, toggle: () => void, title: string, detail: string) => (
    <label className={cx("flex cursor-pointer gap-3 rounded-xl border px-3.5 py-3", checked ? "border-brand bg-mint" : "border-[#DCE3DF] bg-white")}>
      <input type="checkbox" checked={checked} onChange={toggle} className="mt-0.5 h-5 w-5 accent-[#5BA58C]" />
      <span>
        <span className="block text-[13px] font-bold">{title}</span>
        <span className="mt-0.5 block text-xs leading-relaxed text-muted">{detail}</span>
      </span>
    </label>
  );

  return (
    <Screen title="필수 동의" onBack={() => setStep("info")} step="2/2"
      footer={<Button block disabled={!delegation || !lockNotice || busy} onClick={() => void create()}>{busy ? "지갑 만드는 중…" : "동의하고 지갑 만들기"}</Button>}>
      <Card>
        <div className="mb-3 flex items-center justify-between">
          <p className="text-sm font-bold">두 항목 모두 동의가 필요해요</p>
          <Badge>{Number(delegation) + Number(lockNotice)}/2</Badge>
        </div>
        <div className="space-y-2">
          {checkbox(delegation, () => setDelegation(!delegation), "1. 자동 매도 위임",
            "결제 금액만큼 회사가 내 지갑 속 코인을 대신 팔 수 있다는 데 동의합니다.")}
          {checkbox(lockNotice, () => setLockNotice(!lockNotice), "2. 지갑 잠금 안내",
            "결제가 정산되는 몇 초 동안 지갑 전체가 잠기고, 그동안 다른 결제와 출금이 멈추는 것을 확인했습니다.")}
        </div>
      </Card>
      <div className="mt-3">
        <Notice>거래소 계정(예: 바이낸스)이나 외부 개인 지갑의 자산에는 접근하지 않아요. 내가 입금한 만큼만 결제에 쓰여요.</Notice>
      </div>
      {name && <div className="mt-3"><Row label="가입자" value={`${name} · ${assets.join(", ")}`} /></div>}
      {error && <div className="mt-3"><Notice tone="red">{error}</Notice></div>}
      {onBack && <button type="button" className="mt-4 w-full text-center text-xs text-muted underline" onClick={onBack}>취소</button>}
    </Screen>
  );
}
