export type Asset = "BTC" | "ETH" | "USDC" | "USDT";
export const ASSETS: Asset[] = ["BTC", "ETH", "USDC", "USDT"];
export const ASSET_NAMES: Record<Asset, string> = { BTC: "비트코인", ETH: "이더리움", USDC: "USD 코인", USDT: "테더" };

export type WalletAsset = {
  asset: Asset; name: string; value_krw: number; quantity: string; price_krw: number;
  sellable_today_krw: number; stablecoin: boolean;
};

export type Wallet = {
  mode: "simulated" | "external"; wallet_id: string | null; address?: string; total_krw: number | null;
  sellable_today_krw?: number; assets: WalletAsset[]; locked: boolean; lock_checkout_id?: string | null;
  lock_amount_krw?: number | null; hold_expires_at?: string | null; prices_note?: string;
};

export type CardInfo = { card_alias: string; masked: string; card_token: string | null; kind: "nfc" | "demo" };

export type Me = {
  profile: { customer_id: string; name: string; pay_assets: Asset[]; risk_profile: string; created_at: string | null; engine_mode: string };
  wallet: Wallet;
  cards: CardInfo[];
};

export type Leg = { asset: Asset; value_krw: number; quantity: string };

export type PaymentItem = {
  kind: "PAYMENT"; id: string; checkout_id: string; merchant: string; amount_krw: number; status: string;
  authorization_status: string; reason_code: string | null; hold_status?: string | null; liquidation_status: string;
  settlement_status: string; recovery_status?: string | null; sold: Leg[]; sold_krw: number;
  excess_returned_usdc?: string | null; excess_returned_krw: number; created_at: string; financial_status_reported?: boolean;
};

export type WalletItem = {
  kind: "DEPOSIT" | "WITHDRAWAL"; id: string; asset: Asset; amount_krw: number; quantity: string;
  address: string | null; created_at: string;
};

export type ActivityItem = PaymentItem | WalletItem;

export type Enrollment = {
  enrollment_id: string; customer_id: string; terminal_id: string;
  status: "PENDING" | "COMPLETED" | "FAILED" | "EXPIRED" | "CANCELLED";
  card_alias: string | null; masked: string | null; card_token: string | null; error: string | null;
  expires_at: string; engine_registration_required: boolean; customer_name?: string;
};

export type SellPreview = {
  amount_krw: number; required_krw: number; margin: string; total_krw: number;
  status: "OK" | "LIMIT_EXCEEDED" | "EXECUTION_LIMITED"; legs: Leg[]; locked: boolean;
};

export const DEMO_PRICES: Record<Asset, number> = { BTC: 140_000_000, ETH: 5_200_000, USDC: 1_400, USDT: 1_400 };
