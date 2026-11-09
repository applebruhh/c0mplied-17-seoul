import type { ButtonHTMLAttributes, ReactNode } from "react";

export const krw = new Intl.NumberFormat("ko-KR");
export const formatKrw = (value: number | null | undefined) => (value == null ? "—" : `₩${krw.format(value)}`);
export const formatWon = (value: number | null | undefined) => (value == null ? "—" : `${krw.format(value)}원`);

export function cx(...names: Array<string | false | null | undefined>) {
  return names.filter(Boolean).join(" ");
}

export function formatTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  return date.toLocaleString("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

type Variant = "primary" | "secondary" | "ghost" | "danger" | "dark";
const variants: Record<Variant, string> = {
  primary: "bg-brand text-white hover:bg-brand-strong disabled:bg-[#B9C7C1]",
  secondary: "bg-mint text-brand-deep hover:bg-[#DCEEE6] disabled:opacity-50",
  ghost: "bg-transparent text-brand-deep hover:bg-black/5 disabled:opacity-50",
  danger: "bg-danger text-white hover:bg-[#C03A3A] disabled:opacity-50",
  dark: "bg-brand-deep text-white hover:bg-brand-ink disabled:opacity-50",
};

export function Button({ variant = "primary", className, block, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; block?: boolean }) {
  return (
    <button
      type="button"
      {...props}
      className={cx("inline-flex items-center justify-center gap-2 rounded-xl px-4 py-3 text-sm font-semibold transition-colors disabled:cursor-not-allowed",
        variants[variant], block && "w-full", className)}
    />
  );
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  const custom = className?.split(/\s+/).some((name) => name.startsWith("bg-"));
  return <section className={cx("rounded-2xl p-4 shadow-card", !custom && "bg-white", className)}>{children}</section>;
}

type Tone = "green" | "red" | "gray" | "amber" | "dark";
const tones: Record<Tone, string> = {
  green: "bg-mint text-mint-text",
  red: "bg-danger-pale text-danger",
  gray: "bg-[#EEF1EF] text-muted",
  amber: "bg-warn-pale text-warn",
  dark: "bg-brand-deep text-white",
};

export function Badge({ tone = "green", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return <span className={cx("inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold", tones[tone], className)}>{children}</span>;
}

export function Notice({ tone = "green", title, children }: { tone?: "green" | "red" | "amber"; title?: ReactNode; children: ReactNode }) {
  const style = tone === "red" ? "border-danger-line bg-danger-pale text-[#8E2B2B]"
    : tone === "amber" ? "border-[#F0D9A8] bg-warn-pale text-[#7A5313]" : "border-mint-line bg-mint text-brand-deep";
  return (
    <div className={cx("rounded-xl border px-3.5 py-3 text-[13px] leading-relaxed", style)} role={tone === "red" ? "alert" : undefined}>
      {title && <p className="mb-0.5 font-semibold">{title}</p>}
      {children}
    </div>
  );
}

export function Row({ label, value, strong }: { label: ReactNode; value: ReactNode; strong?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl bg-canvas px-3.5 py-3 text-[13px]">
      <span className="text-muted">{label}</span>
      <span className={cx("text-right tabular", strong ? "font-bold text-brand-ink" : "font-medium text-brand-ink")}>{value}</span>
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <span aria-hidden className={cx("inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-r-transparent", className)} />;
}

const assetColors: Record<string, string> = { BTC: "#F7931A", ETH: "#627EEA", USDC: "#2775CA", USDT: "#26A17B" };
const assetGlyph: Record<string, string> = { BTC: "₿", ETH: "Ξ", USDC: "$", USDT: "₮" };

export function AssetIcon({ asset, size = 36 }: { asset: string; size?: number }) {
  return (
    <span className="inline-flex shrink-0 items-center justify-center rounded-full font-bold text-white"
      style={{ width: size, height: size, background: assetColors[asset] ?? "#5BA58C", fontSize: size * 0.45 }} aria-hidden>
      {assetGlyph[asset] ?? asset[0]}
    </span>
  );
}

export function NfcIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className={className} aria-hidden>
      <path d="M6 8.3a6 6 0 0 1 0 7.4" /><path d="M9.5 6a10 10 0 0 1 0 12" /><path d="M13 4a14 14 0 0 1 0 16" /><path d="M16.5 2.5a17 17 0 0 1 0 19" />
    </svg>
  );
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string | undefined, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown; idempotencyKey?: string } = {}): Promise<T> {
  const { json, idempotencyKey, headers, ...rest } = init;
  const response = await fetch(path, {
    cache: "no-store",
    ...rest,
    signal: rest.signal ? AbortSignal.any([rest.signal, AbortSignal.timeout(10_000)]) : AbortSignal.timeout(10_000),
    headers: {
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      ...(headers as Record<string, string> | undefined),
    },
    ...(json !== undefined ? { body: JSON.stringify(json) } : {}),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { error?: { code?: string; message?: string }; detail?: unknown } | null;
    const message = body?.error?.message ?? (response.status >= 500 ? "서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요." : `요청이 거절됐어요 (HTTP ${response.status}).`);
    throw new ApiError(response.status, body?.error?.code, message);
  }
  return response.json() as Promise<T>;
}

export function errorText(error: unknown) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof DOMException && (error.name === "TimeoutError" || error.name === "AbortError")) return "응답이 늦어지고 있어요. 다시 시도해 주세요.";
  return "서버에 연결할 수 없어요. 노트북의 VaultPay 서버와 연결 상태를 확인해 주세요.";
}

export const reasonText: Record<string, string> = {
  LIMIT_EXCEEDED: "잔액이 부족해요",
  WALLET_BUSY: "이전 결제를 처리 중이에요",
  UNKNOWN_WALLET: "등록되지 않은 카드예요",
  UNKNOWN_CARD: "등록되지 않은 카드예요",
  PRICE_UNAVAILABLE: "시세를 불러올 수 없어요",
  EXECUTION_LIQUIDITY_UNAVAILABLE: "오늘 매도 가능한 한도가 부족해요",
  PREFUNDING_INSUFFICIENT: "회사 결제 한도가 부족해요",
  DEMO_MERCHANT_PAYOUT_FAILED: "가맹점 송금에 실패했어요",
};
