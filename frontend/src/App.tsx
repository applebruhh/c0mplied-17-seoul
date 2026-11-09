import { useEffect, useState } from "react";
import MerchantPos from "./features/pos/MerchantPos";

const routes = [
  { path: "/pos", label: "Merchant POS" },
  { path: "/terminal", label: "NFC terminal", title: "One tap starts the journey.", detail: "The Galaxy terminal will read an NDEF demo card and send its identifier to the backend.", next: "Web NFC scanning, permission handling, and active checkout lookup" },
  { path: "/ops", label: "Operations", title: "Approval is just the beginning.", detail: "A read-only view will follow asset reservations, KRW prefunding, liquidation, and reimbursement.", next: "Settlement timeline and recovery visibility" },
  { path: "/demo", label: "Presentation", title: "From the first tap to final settlement.", detail: "The presentation view will bring the merchant checkout and operations timeline together.", next: "Combined live POS and operations views" },
];

export default function App() {
  const path = window.location.pathname;
  const current = routes.find((route) => route.path === path) ?? (path === "/" ? routes[0] : null);
  const [health, setHealth] = useState("Connecting to local backend…");

  useEffect(() => {
    if (current?.path === "/pos") return;
    const controller = new AbortController();
    fetch("/api/v1/health", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Backend unavailable");
        const result: unknown = await response.json();
        if (typeof result !== "object" || result === null || !("status" in result) || result.status !== "ok") {
          throw new Error("Unexpected health response");
        }
        setHealth("Local backend connected");
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) setHealth("Backend offline · start FastAPI on port 8000");
      });
    return () => controller.abort();
  }, [current?.path]);

  return (
    <div className="min-h-screen bg-[#f3f5ef] text-[#14251f]">
      <header className="border-b border-[#d6ded4] bg-white/70">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-5 px-6 py-6">
          <a className="text-2xl font-bold tracking-tight" href="/pos">▣ VaultPay<span className="ml-2 text-xs font-normal tracking-normal text-[#65766d]">LOCAL DEMO</span></a>
          <nav aria-label="Main navigation" className="flex flex-wrap gap-2">
            {routes.map((route) => <a key={route.path} href={route.path} aria-current={route.path === current?.path ? "page" : undefined} className={`rounded-full px-4 py-2 text-sm ${route.path === current?.path ? "bg-[#163d30] text-white" : "hover:bg-[#e5ece2]"}`}>{route.label}</a>)}
          </nav>
        </div>
      </header>
      {current?.path === "/pos" ? <MerchantPos /> : <main className="mx-auto max-w-6xl px-6 py-16 md:py-24">
        <p className="mb-5 text-xs font-semibold uppercase tracking-[0.2em] text-[#526e5b]">{current?.label ?? "Page not found"} / Foundation 01</p>
        <h1 className="max-w-3xl text-4xl font-medium leading-tight tracking-tight md:text-6xl">{current?.title ?? "This route does not exist."}</h1>
        <p className="mt-6 max-w-2xl text-lg leading-relaxed text-[#5c7064]">{current?.detail ?? "Choose a destination from the navigation."}</p>
        <div className="mt-12 grid gap-5 md:grid-cols-2">
          <section className="rounded-2xl border border-[#d4ded1] bg-white p-7">
            <p className="text-xs font-semibold uppercase tracking-widest text-[#647761]">Implementation status</p>
            <h2 className="mt-4 text-xl font-medium">Project scaffold ready</h2>
            <p className="mt-3 leading-relaxed text-[#607063]">This is a route shell. Payments, NFC scanning, and live events are scheduled for the next implementation milestones.</p>
            {current && <p className="mt-5 border-t border-[#e1e7dd] pt-5 text-sm">Next: {current.next}.</p>}
          </section>
          <section className="rounded-2xl bg-[#163d30] p-7 text-white">
            <p className="text-xs uppercase tracking-widest text-[#bdceba]">Architecture principle</p>
            <h2 className="mt-4 text-xl font-medium">Fast approval. Background settlement.</h2>
            <p className="mt-3 leading-relaxed text-[#c9dac9]">The engine reserves eligible crypto and company KRW liquidity before authorization. Liquidation and reconciliation follow asynchronously.</p>
            <p className="mt-5 text-xs text-[#bdceba]">SUPPORTED ASSETS · ETH / BTC / USDC</p>
          </section>
        </div>
        <p role="status" className="mt-8 text-sm text-[#5d7061]">{health}</p>
      </main>}
      <footer className="mx-auto max-w-6xl border-t border-[#d6ded4] px-6 py-6 text-xs leading-relaxed text-[#65766d]">Hackathon demonstration · Simulated KRW prefunding and card authorization · No real payment processing</footer>
    </div>
  );
}
