# Implementation milestones

## Phase 1 — completed
Architecture, initial API/state design and React/FastAPI scaffolding.

## Phase 2 — completed
SQLAlchemy persistence, checkout creation/get/active/cancel/expiry, NFC card/terminal validation, transaction-level deduplication, adapter boundary, separate financial projections, durable SSE, operations endpoints, protected reset and backend tests. The architecture clarification separated prefunding reservation from completed merchant payout and documented engine/Embedded Wallet ownership.

## Phase 3 — Mock Payment Engine
- [x] Preserve POS routes, checkout/tap bodies and existing financial projections.
- [x] Durable prepaid wallet snapshots for BTC/ETH/USDC/USDT, whole-wallet atomic locks and local spending/deposit fixture rules.
- [x] Margin/total-value checks distinct from executable fixture capacity and company prefunding failures.
- [x] Async static router fixtures with bounded submissions/retries/slippage and daily non-stablecoin caps.
- [x] Scripted treasury reimbursement, excess USDC return, exposure and reconciled LOSS_ABSORBED release.
- [x] Hold TTL expiry before submission; unknown outcomes retain locks until reconciliation.
- [x] Durable engine event log and exactly-once projection into the existing outbox per received event ID/version.
- [x] Policy/deadline snapshots, restart persistence and non-destructive Phase 2 UNKNOWN-constraint upgrade.
- [x] API/state documentation, scenario fixtures and pytest coverage.

No actual external engine, wallet custody, swaps, bank transfers, v5 formula or subscription optimization is implemented. The shared-contract preparation now supplies an opt-in external authorization transport; default app startup remains mock-only. Pending team confirmations are listed in architecture.md and api-contract.md.

## Later — terminal and merchant UI
Wire the existing /api contracts into amount entry, history, server-driven states, consecutive transactions and SSE reconnect. Implement Galaxy Web NFC NDEF parsing, permission/error states and user-gesture scanning. Always send known checkout_id. Show whole-wallet busy versus terminal availability and explicit simulated provenance.

## Later — operations and presentation UI
Show hold deadline, pending/unknown transactions, route attempts, treasury reimbursement, unrecovered exposure and resolved company losses. Distinguish recovery PENDING from LOSS_ABSORBED without changing the authorized purchase. Add the combined demo view; operations stays read-only.

## Later — real team integration
Confirm authenticated engine paths, checkout-key idempotency/lookup and downstream guarantees, actual v5 response fixtures, swap/retry budget semantics, UTC-day valuation policy, treasury receipts and recovery. Only then implement HTTPX calls in the external adapter and add team contract fixtures. Production authentication and real financial services remain outside this repository's local demo scope.


## Shared-contract preparation — completed before parallel UI work
Frozen vaultpay.pos.v1 Pydantic/TypeScript/JSON Schema/OpenAPI artifacts, exact UID/HMAC vectors, opt-in external authorization/status transport and immutable request snapshots. See parallel-development.md for Phase 4 terminal, Phase 5 POS and Phase 6 ops/presentation lane ownership. GET schema/auth/404 absence semantics and external financial detail mapping remain open; they must be agreed by the shared integration owner rather than invented by UI lanes.
