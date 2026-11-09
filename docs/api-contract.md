# VaultPay API contract · Phase 3 Mock Payment Engine

The finalized Phase 3 specification supersedes older assumptions about supported assets, lock release after failure and terminal recovery. Existing POS paths, checkout/tap request bodies, checkout states and response envelopes remain intact. The adapter continues to expose only authorize and get_payment; there are no POS-to-Wallet or POS-to-DEX HTTP calls.

## HTTP conventions and existing routes
All public paths use `/api`; `/api/v1/health` and `/api/v1/capabilities` remain health-shell compatibility aliases. Swagger/OpenAPI are at `/docs` and `/openapi.json`. Strict positive integer KRW amounts are bounded to 1–10,000,000; timestamps are UTC ISO-8601. Reject extra command fields. Domain errors use `{error:{code,message,request_id}}`; validation uses FastAPI 422 detail. UUID IDs are validated.

| Method/path | Request/result |
| --- | --- |
| GET /api/health | Process availability, not external financial health |
| GET /api/capabilities | Demo flags; BTC, ETH, USDC, USDT |
| POST /api/checkouts | `{merchant_id,terminal_id,amount_krw,expires_in_seconds?:300}`; 201 Checkout, 200 creation replay |
| GET /api/checkouts/{checkout_id} | Checkout with latest embedded Payment projection |
| GET /api/terminals/{terminal_id}/active-checkout | `{checkout:Checkout|null,latest_event_id}` |
| POST /api/checkouts/{checkout_id}/cancel | Cancel WAITING_FOR_CARD only; replay harmless |
| POST /api/nfc/tap | `{terminal_id,card_alias,client_event_id,checkout_id?}`; `{checkout,payment,duplicate,latest_event_id}` |
| GET /api/checkouts/{checkout_id}/events | Durable SSE with snapshot/replay/live tail |
| GET /api/ops/payments | `{items,total,limit,offset,latest_event_id}`; limit 1–100, offset ≥0, optional settlement_status |
| GET /api/ops/payments/{payment_id} | Latest Payment projection |
| GET /api/ops/events | Global SSE, snapshot newest 100 payments plus total |
| POST /api/demo/reset | Protected simulated reset; blocked while authorization or submitted transactions remain unresolved |

Checkout creation optionally accepts a UUID Idempotency-Key header; identical input replays, different input returns 409. Expiry defaults to 300 seconds (1–3600). Registered enabled terminal must belong to the merchant. One WAITING_FOR_CARD or AUTHORIZING checkout per terminal is enforced by a partial unique index. Amount, merchant and terminal are immutable through the API.

Checkout fields remain checkout_id, merchant_id, terminal_id, amount_krw, currency=KRW, status, created_at, expires_at, updated_at, payment_id and payment. States remain WAITING_FOR_CARD, AUTHORIZING, AUTHORIZED, DECLINED, CANCELLED, EXPIRED. Only waiting sessions expire or cancel. Definitive authorization frees the terminal, but **the same customer's entire wallet remains busy until settlement/recovery releases it**.

Tap resolves customer/amount from backend records; client cannot inject balances, amounts or outcomes. Explicit checkout_id is recommended to prevent delayed taps from consuming a later checkout. Omission targets the current active session. Unknown/disabled terminal/card: 404. No active checkout: 409. Explicit expired checkout: 410. A definitive engine decline returns HTTP 200 with DECLINED; in-flight/uncertain authorization returns 202/AUTHORIZING. Card aliases remain clonable demo lookup identifiers, not authentication.

## Idempotency and lock scope
- checkout_id is the engine/payment key. Unique payment.checkout_id and payment.idempotency_key enforce one payment per checkout.
- client_event_id deduplicates accepted NFC events. Same ID/body returns the same payment; changed body returns 409. New event IDs for the same checkout/customer also converge on its payment.
- The mock atomically acquires the entire wallet and reads its deposited spendable snapshot in a BEGIN IMMEDIATE transaction. Concurrent payments on different terminals/checkouts for one customer produce one approval and one WALLET_BUSY decline.
- Mock request replay reads the same durable engine record; it cannot repeat the hold, merchant payout or liquidation submission. get_payment advances only persisted, due steps. Every submission has a stable ID, bounded attempt counter, and recorded outcome. Repeated settled polls leave balances, capacity, events and versions unchanged.
- Incoming deposits remain in the same locked wallet and never unlock it. Local mock deposit/withdraw/spend test helpers are not POS wallet APIs. Outgoing operations on any asset are blocked while locked and have their own operation-key deduplication.

## Payment projection and states
Existing fields remain: payment_id, checkout_id, idempotency_key, merchant_id, terminal_id, customer_id, amount_krw, currency, authorization_status, asset_reservation_status, prefunding_status, merchant_payout_status, liquidation_status, reimbursement_status, settlement_status, source, engine_payment_id, engine_version, reason_code, last_error, assets, timestamps, settlement_reference, created_at, updated_at, authorized_at and settled_at.

Additive fields: wallet_id, hold_status, hold_expires_at, recovery_status, transaction_status, required_krw, unpaid_exposure_krw, loss_absorbed_krw, excess_returned_usdc and mock_asset_metadata. Optional legacy fields are null/empty when unreported; unknown values are not invented. New mock payments populate them. Monetary exposure uses integer KRW. excess_returned_usdc is a decimal string based on a fixed **simulated** 1 USDC=1,400 KRW quote.

| Field | Values |
| --- | --- |
| authorization_status | PENDING, UNKNOWN, AUTHORIZED, DECLINED |
| hold_status | NOT_LOCKED, LOCKED, RELEASED, EXPIRED |
| recovery_status | PENDING, REIMBURSED, LOSS_ABSORBED |
| transaction_status | NOT_SUBMITTED, PENDING, UNKNOWN, CONFIRMED, FAILED |
| asset_reservation_status | Existing NOT_STARTED, PENDING, RESERVED, FAILED, RELEASED projection retained |
| prefunding_status | Existing NOT_STARTED, PENDING, RESERVED, FAILED |
| merchant_payout_status | Existing optional NOT_STARTED, PENDING, COMPLETED, FAILED |
| liquidation_status | NOT_STARTED, PENDING, PROCESSING, COMPLETED, FAILED |
| reimbursement_status | NOT_STARTED, PENDING, COMPLETED, FAILED |
| settlement_status | Existing NOT_STARTED, PENDING, COMPLETED, RECOVERY_REQUIRED |

Hold is the authoritative whole-wallet lock projection; asset_reservation_status remains for frontend compatibility. EXPIRED maps to a released service reservation, with HOLD_EXPIRED history. UNKNOWN authorization maps to checkout AUTHORIZING and is reconciled. A transport timeout records UNKNOWN; malformed engine responses remain unapproved rather than becoming declines. Final AUTHORIZED/DECLINED decisions cannot reverse.

New approvals retain the prior explicit completed merchant-payout condition, after the hold and prefunding checks. Prefunding RESERVED is separate from payout COMPLETED. Payout failure is definitively declined, simulated unused capacity is returned and the wallet is explicitly released. Old authorized rows without payout metadata remain readable without fabricated payout evidence; they are not paid again.

## Mock authorization policy
Only seeded prepaid wallet holdings count; external assets and stocks/tokenized stocks do not. Under one transaction: identify wallet → acquire exclusive lock/snapshot (otherwise WALLET_BUSY) → calculate `ceil(amount_krw * (1 + MARGIN))` → compare total deposited KRW-valued balance → check deterministic fixture execution feasibility → check/reserve simulated company capacity → HOLD_PLACED → mock merchant payout → AUTHORIZED. No liquidation or swap is awaited.

| Reason | Meaning |
| --- | --- |
| WALLET_BUSY | Existing exclusive lock; the losing request never releases its owner's lock |
| LIMIT_EXCEEDED | Total deposited wallet value is below required_krw; release the acquired lock |
| EXECUTION_LIQUIDITY_UNAVAILABLE | Total is sufficient, but the configured fixture routes/caps/leg budget cannot execute enough; never relabel as LIMIT_EXCEEDED |
| PREFUNDING_INSUFFICIENT | Company simulated KRW capacity unavailable; release wallet |
| DEMO_MERCHANT_PAYOUT_FAILED | Definitive simulated payout failure before approval; release wallet and unused company capacity |

## Deterministic router fixtures and limits
`mock_asset_metadata` exposes fixture_version=v5-demo-1, hand-authored rankings, deposited snapshot, daily basis/day, margin/slippage settings and submissions (ID, asset, route, attempt, input KRW value, maximum/quoted slippage, timestamp and status). These illustrative scores/routes represent v5 output shapes; they are **not actual v5 outputs or an approximation of its formula**.

Default ranking is BTC → ETH → USDC → USDT; stablecoins are not forced first. A simple fixed-order fixture allocator checks capacity and fills the mocked requirement. It does not optimize. Only positive-capacity assets count as legs.

**Limit interpretation pending final team confirmation:** at most **3 total swap submissions per payment, including retries**, and therefore at most 3 distinct legs. Each logical asset/route swap permits at most 2 retries (3 submissions), subject to that shared budget. A fallback consumes the same budget; it never starts a new budget. Examples: BTC initial + 2 BTC retries uses all 3; BTC initial failure + ETH initial failure + ETH retry also uses all 3; three successful distinct legs allow no retries. Exhaustion is a known liquidation failure.

Submitted maximum slippage cannot exceed 0.005 (0.5%). High-slippage fixtures simulate a rejected/reverted swap, never a fill beyond that bound. Known failed submissions consume the attempt budget but neither sell assets nor consume daily sell capacity. Only confirmed swaps debit the mock snapshot and record proceeds. Non-stablecoin confirmed sells are capped at 30% of the wallet's UTC-day opening fixture value per asset; prior same-day sells reduce capacity. Stablecoins have no percentage cap. The budget day is fixed while a wallet is locked; a new unlocked-day payment refreshes the basis. New deposits cannot increase the current day's non-stablecoin cap. No market pricing, gas, custody or real daily financial ledger is modeled.

## Recovery, TTL and unknown outcomes
Successful confirmed liquidation yields simulated USDC, records treasury reimbursement for amount_krw, returns all excess proceeds to the customer as simulated USDC, marks REIMBURSED and releases the whole wallet. Company capacity is replenished only on successful reimbursement. Rounding: required KRW up, per-swap proceeds down, displayed USDC down to 8 decimals; KRW snapshot values are authoritative demo units.

Known liquidation failure exhausts configured retries/fallbacks, records LIQUIDATION_FAILED and unpaid company exposure, and remains LOCKED with recovery PENDING. A later reconciliation step applies the demo policy LOSS_ABSORBED, retains AUTHORIZED, records loss_absorbed_krw and explicitly releases the wallet. settlement_status stays RECOVERY_REQUIRED to retain the failure history; recovery_status distinguishes a reconciled loss from pending recovery. No authorization becomes FAILED. Unpaid exposure remains visible even after the company absorbs the loss.

**Demo loss policy assumption:** the company writes off the merchant amount, keeps its reduced simulated capacity, and returns any confirmed liquidation proceeds still undelivered to treasury to the customer as simulated USDC. The reimbursement-failure fixture is a definitively failed treasury delivery, not an unknown transfer; it has no treasury_received_at. No retry/compensation is sent to a real wallet or bank.

If HOLD_TTL expires **before any submission**, a known authorized purchase records HOLD_EXPIRED, exposure and LOSS_ABSORBED, then releases the service lock as EXPIRED. No swap is submitted. Authorization remains intact. A pending/unknown submitted transaction never expires into an unlock; the mock waits for its deterministic reconciliation result, even beyond TTL. An unknown merchant payout is likewise not automatically unlocked; after confirmation, a passed pre-submission deadline follows the expiry/exposure policy. Incoming deposits do not change this rule.

## SSE and persisted event log
Existing SSE options remain Last-Event-ID (takes precedence), ?after=<cursor> and ?follow=false for finite Swagger/curl responses. Replay to a consistent snapshot cursor, send latest snapshot, then tail SQLite. Heartbeats every 15 seconds. Clients deduplicate event IDs and apply snapshots even if the last replayed ID matches. Future cursor produces resync. No in-memory subscription is required. Ops snapshots include latest 100 payments; paginate the list for more.

Existing lowercase checkout/payment/projection events remain, including merchant_payout.status_changed. Additional simulated engine types include WALLET_LOCKED, HOLD_PLACED, AUTHORIZATION_DECLINED, AUTHORIZATION_UNKNOWN, MERCHANT_PAYOUT_COMPLETED, AUTHORIZED, LIQUIDATION_STARTED, ASSET_SELECTED, DEX_SWAP_SUBMITTED, TRANSACTION_UNKNOWN, DEX_SWAP_FAILED, DEX_SWAP_CONFIRMED, LIQUIDATION_COMPLETED, USDC_RECEIVED_BY_TREASURY, REIMBURSEMENT_COMPLETED, LIQUIDATION_FAILED, REIMBURSEMENT_FAILED, EXPOSURE_RECORDED, LOSS_ABSORBED, HOLD_EXPIRED and HOLD_RELEASED.

Engine logs persist event_id, payment_id, checkout_id, customer_id, wallet_id, event_type, timestamp, state, mock asset metadata and recovery outcome in mock_engine_events. POS SSE persists each event once in its existing outbox with source=simulated, its own integer cursor and payload `{checkout, mock_engine_event:{...}}`. Correlated engine IDs remain inside the payload. Intermediate engine state is under mock_engine_event.state; payload.checkout is the latest aggregate projection when the backend received the batch. Duplicate engine versions/events do not create duplicate effects or logs.

## Settings, reset and compatibility
Defaults: MARGIN=0.03, MAX_SLIPPAGE=0.005, HOLD_TTL=300 seconds. Process env: VAULTPAY_MOCK_MARGIN, VAULTPAY_MOCK_MAX_SLIPPAGE (0–0.005), VAULTPAY_MOCK_HOLD_TTL (use 60 for demos), VAULTPAY_MOCK_UNKNOWN_SECONDS (5), VAULTPAY_MOCK_STAGE_SECONDS (1). Policies/deadlines are snapshotted per payment; restarts do not replace an in-flight policy with new defaults.

Reset requires VAULTPAY_DEMO_RESET_TOKEN / X-Demo-Reset-Token; unset=503, wrong/missing=403. Unresolved authorization or pending/unknown submitted swaps block reset (409). Reset clears simulated payment/wallet/engine history, reseeds prepaid wallets/capacity and demo cards, retains registration and emits a higher demo.reset cursor. This is a manual fixture reset, never a financial reversal. This server remains a trusted localhost demo without production merchant/ops authentication.

Startup creates additive mock tables and performs a narrow transactional payments CHECK-constraint rebuild for UNKNOWN on Phase 2 databases, preserving row IDs, tap/event references, indexes and triggers with foreign-key validation. schema.sql is a generated reference. Phase 1 schemas are still refused. Legacy flat engine records retain their old read/reconciliation behavior and are not retroactively assigned wallet locks or Phase 3 losses.

## External integration boundary and assumptions
PaymentAdapter signatures stay `authorize(AuthorizationRequest)` and `get_payment(checkout_id)`. EnginePayment adds optional mock/hold/recovery projections and a mock_events list; ordinary external responses may omit mock-only fields. The actual ExternalPaymentAdapter remains unchanged and disabled. There are no real transfers, subscriptions, AI trading models or v5 optimization here.

The team must still confirm authenticated engine paths, payout evidence, authoritative same-key lookup and absence semantics, downstream idempotency for wallet/merchant/DEX instructions, this conservative 3-submission budget interpretation, actual v5 fixture data, daily-limit valuation/day rules and the precise external loss/refund policy. EnginePaymentNotFound guarantees definitive absence; only then may the worker resubmit the same checkout key. Other transport errors remain uncertain and never generate a new key.
