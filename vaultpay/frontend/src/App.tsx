import { useEffect, useState } from "react";
import CustomerApp from "./features/customer/CustomerApp";
import OperationsPage from "./features/ops/OperationsPage";
import MerchantPos from "./features/pos/MerchantPos";
import Terminal from "./features/terminal/Terminal";
import { Badge, cx, NfcIcon } from "./ui/kit";

const deskRoutes = [
  { path: "/pos", label: "가게 POS" },
  { path: "/ops", label: "운영 콘솔" },
  { path: "/", label: "화면 안내" },
];

function useHealth() {
  const [state, setState] = useState<{ ok: boolean; demo?: boolean } | null>(null);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function check() {
      try {
        const [health, capabilities] = await Promise.all([
          fetch("/api/v1/health", { cache: "no-store", signal: AbortSignal.timeout(4000) }),
          fetch("/api/capabilities", { cache: "no-store", signal: AbortSignal.timeout(4000) }),
        ]);
        const body = await capabilities.json().catch(() => ({}));
        if (active) setState({ ok: health.ok, demo: body?.demo_only });
      } catch {
        if (active) setState({ ok: false });
      }
      if (active) timer = setTimeout(check, 10_000);
    }
    void check();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);
  return state;
}

function Hub() {
  const origin = window.location.origin;
  const screens = [
    { path: "/pos", device: "가게 노트북", title: "가게 POS 화면", detail: "금액을 입력하면 갤럭시 단말기가 카드를 기다리고, 승인·거절이 크게 떠요.", tone: "bg-brand text-white" },
    { path: "/terminal", device: "갤럭시 (USB 연결)", title: "NFC 카드 리더", detail: "휴대폰 뒷면에 카드를 대면 UID를 읽어 결제·카드 등록을 처리해요.", tone: "bg-brand-deep text-white" },
    { path: "/app", device: "고객 휴대폰", title: "BlockBucks 고객 앱", detail: "가입 · 지갑 생성 · 코인 충전 · 카드 등록 · 결제/차감 내역.", tone: "bg-white" },
    { path: "/ops", device: "가게 노트북 / 운영자", title: "운영 콘솔", detail: "결제 4개 상태 축, 스왑 내역, 지갑 잠금, 손실 흡수, 고객 지갑.", tone: "bg-white" },
  ];
  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <p className="text-sm font-semibold text-mint-text">BlockBucks · VaultPay 데모</p>
      <h1 className="mt-2 text-4xl font-extrabold tracking-tight">카드 한 장으로, 코인으로 결제</h1>
      <p className="mt-3 max-w-2xl text-muted">갤럭시 휴대폰이 카드 리더가 되고, 연결된 노트북에는 가게 화면이, 다른 휴대폰에는 고객 앱이 떠요.</p>
      <div className="mt-8 grid gap-4 md:grid-cols-2">
        {screens.map((screen) => (
          <a key={screen.path} href={screen.path} className={cx("group rounded-3xl p-6 shadow-card transition-transform hover:-translate-y-0.5", screen.tone)}>
            <p className="text-xs font-semibold uppercase tracking-wider opacity-70">{screen.device}</p>
            <p className="mt-2 text-2xl font-extrabold">{screen.title}</p>
            <p className="mt-2 text-sm opacity-80">{screen.detail}</p>
            <p className="mt-4 font-mono text-sm opacity-80">{origin}{screen.path} →</p>
          </a>
        ))}
      </div>
      <section className="mt-8 rounded-3xl bg-white p-6 shadow-card">
        <h2 className="flex items-center gap-2 text-lg font-bold"><NfcIcon className="h-5 w-5 text-brand" />기기 연결 순서</h2>
        <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm leading-relaxed">
          <li>노트북에서 서버 실행 후 이 페이지(<span className="font-mono">{origin}</span>)가 열리는지 확인</li>
          <li>갤럭시: NFC·USB 디버깅 켜고 USB 연결 → <code className="rounded bg-canvas px-1.5">adb reverse tcp:8000 tcp:8000</code> → Chrome에서 <span className="font-mono">http://localhost:8000/terminal</span></li>
          <li>고객 휴대폰: 두 번째 USB 연결 후 <code className="rounded bg-canvas px-1.5">adb -s &lt;기기ID&gt; reverse tcp:8000 tcp:8000</code> → <span className="font-mono">http://localhost:8000/app</span> (같은 Wi-Fi라면 노트북 IP:8000/app도 가능)</li>
          <li>고객 앱에서 가입 → 충전 → ‘카드 등록 → 매장 단말기’ 요청 → 갤럭시에 카드 태그</li>
          <li>노트북 POS에서 금액 입력 → 갤럭시에서 ‘NFC 스캔 시작’ → 카드 태그 → POS·고객 앱에 결과 표시</li>
        </ol>
      </section>
    </main>
  );
}

export default function App() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const health = useHealth();
  if (path === "/terminal") return <Terminal />;
  if (path === "/app") return <CustomerApp />;
  const known = deskRoutes.some((route) => route.path === path);

  return (
    <div className="min-h-screen bg-canvas text-brand-ink">
      <header className="sticky top-0 z-20 border-b border-[#DCE3DF] bg-white/90 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-6">
          <a className="flex items-center gap-2 text-xl font-extrabold tracking-tight text-brand-deep" href="/">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand text-sm text-white">B</span>BlockBucks
          </a>
          <nav aria-label="주요 메뉴" className="flex items-center gap-1">
            {deskRoutes.map((route) => (
              <a key={route.path} href={route.path} aria-current={route.path === path ? "page" : undefined}
                className={cx("rounded-full px-4 py-2 text-sm font-semibold", route.path === path ? "bg-brand-deep text-white" : "text-muted hover:bg-canvas")}>{route.label}</a>
            ))}
          </nav>
          <span role="status" className="hidden items-center gap-2 text-xs sm:flex">
            <span className={cx("h-2 w-2 rounded-full", health?.ok ? "bg-brand" : health ? "bg-danger" : "bg-[#C9D2CE]")} aria-hidden />
            {health?.ok ? "서버 연결됨" : health ? "서버 오프라인 · 8000번 포트 확인" : "연결 확인 중"}
            {health?.ok && health.demo !== undefined && <Badge tone={health.demo ? "green" : "amber"}>{health.demo ? "시뮬레이션" : "외부 엔진"}</Badge>}
          </span>
        </div>
      </header>
      {path === "/ops" ? <OperationsPage /> : path === "/pos" ? <MerchantPos /> : path === "/" ? <Hub /> : (
        <main className="mx-auto max-w-6xl px-6 py-20 text-center">
          <p className="text-3xl font-bold">페이지를 찾을 수 없어요</p>
          <a className="mt-4 inline-block text-brand underline" href="/">화면 안내로 가기</a>
        </main>
      )}
      {known && <footer className="mx-auto max-w-7xl px-6 py-6 text-xs text-muted">해커톤 데모 · 결제 엔진이 mock 모드면 원화 선지급·코인 매도는 시뮬레이션이에요.</footer>}
    </div>
  );
}
