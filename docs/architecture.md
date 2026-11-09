# VaultPay architecture · Phase 3

## Scope and ownership
Local NFC POS hackathon platform: checkout/NFC APIs, authorization projections, read-only operations and durable SSE. React remains a route scaffold. The MockPaymentAdapter simulates an external Payment Engine; it is not a real financial engine. No custody, on-chain execution, v5 optimization, AI trading, bank transfers or subscription optimizer is implemented.

| Component | Responsibility |
| --- | --- |
| POS Backend | Checkout lifecycle, NFC validation/deduplication, engine API boundary, authorization/status projection and real-time events |
| External Payment Engine | Wallet hold instructions, authorization, company capacity/payout coordination, liquidation instructions and settlement/recovery orchestration |
| Embedded Wallet | Customer crypto custody, deposited spendable balance, whole-wallet service hold and wallet-controlled liquidation |
| Company Treasury | Receives USDC reimbursement; engine reconciles unused reservation release |

Finalized spec adds prepaid BTC/ETH/USDC/USDT snapshots, entire-wallet exclusivity, explicit hold/recovery state and bounded mock route execution. It supersedes the prior three-asset restriction and indefinite hold after any failure. The POS still calls only authorize and get_payment; no direct Wallet, DEX or Treasury HTTP calls exist.

## Modular monolith
FastAPI/Pydantic transport, SQLAlchemy/SQLite persistence, POSService lifecycle/projection guards, adapter protocol, background worker and durable SSE. HTTPX remains for API tests and future external integration. No extra service or queue is required.

```text
backend/app/
  main.py                       Existing API routes and lifecycle
  config.py                     Validated demo settings
  models.py                     Existing contracts + hold/recovery/UNKNOWN projections
  tables.py / schema.sql         ORM schema and generated SQL reference
  migrations.py                 Narrow Phase 2 UNKNOWN-constraint upgrade
  db.py                         SQLite transactions and demo registration/snapshot seeds
  services.py                   POS claims, projection guards and durable outbox
  worker.py / streaming.py       Restart-safe polling and SSE replay/snapshots
  adapters/
    base.py / external.py        Shared engine interface; opt-in signed external transport
    mock.py                     Aggregate durable orchestration simulator
    mock_fixtures.py             Static snapshots, v5-shaped rankings and routes
backend/tests/
  test_backend.py               Existing API/constraints/concurrency tests
  test_mock_engine.py           Lock, route budget, expiry, exposure/loss, unknown and migration tests
  fixtures/phase2-schema.sql     Upgrade compatibility fixture
frontend/                       Existing React/Vite/TypeScript/Tailwind route shells
```

## Atomicity, state and idempotency
SQLite uses foreign keys, WAL, busy timeout and short explicit BEGIN IMMEDIATE write transactions. Reads use explicit BEGIN for consistent snapshots. The one-active-checkout partial index, unique payment checkout/key and unique client_event_id remain. Engine checkout UUID is the idempotency key across all mocked financial effects.

Mock authorization acquires the entire prepaid wallet and inspects the same deposited snapshot atomically. Another checkout cannot lock that wallet: it receives WALLET_BUSY without releasing the current owner's lock. Only the owning payment can release it. All fake balance/capacity updates, state changes, submission IDs and engine events commit together, so a crash cannot separate a confirmed debit from its recorded outcome. Retry reads the same fixture. Duplicate settled polls do nothing, and an old payment cannot release a newer payment's wallet lock.

Authorization checks `ceil(amount * (1 + MARGIN))` against total deposited value, then separately checks execution capacity under the fixed route fixture/caps. Execution constraints produce EXECUTION_LIQUIDITY_UNAVAILABLE, not LIMIT_EXCEEDED. Company capacity/payout is simulated, distinct from wallet funds. New approval still requires reported payout COMPLETED. Neither authorize nor a replay waits for liquidation; only due get_payment steps advance it.

The backend projects all financial fields; it makes no financial decisions for a real engine. UNKNOWN is an unresolved authorization, mapped to checkout AUTHORIZING. New JSON projections add hold_status, recovery_status, transaction_status, required/exposure/loss values and fixture metadata. Existing checkout models, endpoints and financial projection fields remain. A separate common AuthorizationResult now supports the sparse external decision without weakening detailed EnginePayment financial guards; financial_status_reported distinguishes reported detail from local placeholders. See [API contract](api-contract.md) for enum mappings and [payment flow](payment-flow.md) for release policy.

## Durable mock tables
| Table | Purpose |
| --- | --- |
| merchants / terminals / customers / demo_cards | Existing demo registration and lookup |
| checkouts / payments / tap_events / events | Existing checkout state, financial projection, deduplication and SSE outbox |
| authorization_snapshots | Frozen selected-card/amount command, exact external JSON bytes, submission ownership, cached engine decision and local response revision |
| mock_engine_records | Checkout-keyed request and durable projection/runtime/policy snapshot; legacy flat records supported |
| mock_wallets | Prepaid KRW-valued crypto snapshots, exclusive lock owner/deadline, opening-day basis and confirmed sell totals |
| mock_company_capacity | Simulated prefunded capacity, reduced on payout, restored on reimbursement, retained as loss otherwise |
| mock_wallet_operations | Idempotent local deposit/withdraw/spend fixture operations, with outgoing operations blocked under lock |
| mock_engine_events | Immutable correlated event IDs, customer/wallet IDs, timestamp/state/asset metadata/recovery outcome |

Mock wallet helpers are local test/demo methods, not POS-to-Wallet APIs or production financial ledgers. Only deposited fixture holdings count, all four assets share one wallet, deposits do not unlock, and deposited non-stablecoins do not enlarge today's sell cap.

## Settlement, budgets and loss
Router scores/routes are hand-authored v5-shaped fixtures. They are not the complete v5 formula or claimed actual model outputs. Default BTC/ETH-first order does not force stablecoins first. Three **total submissions including retries** is the conservative shared payment budget; at most two retries per individual route/asset. A fallback uses the same budget. Non-stablecoin sells cap at 30% of the opening-day per-asset value; stablecoins have no percentage cap. Submitted maximum slippage is capped at 0.5%. Policy, routes and deadlines are persisted per payment.

Only confirmed swaps debit wallet values. Reimbursement credits company capacity and returns excess to the customer in simulated USDC. On exhausted known failure, record exposure and retain the lock under recovery PENDING. A later reconciliation applies LOSS_ABSORBED and explicitly releases the wallet, preserving AUTHORIZED and historical settlement RECOVERY_REQUIRED. This replaces Phase 2's permanently preserved recovery hold for new mock payments. Legacy recovery records keep their original policy.

HOLD_TTL only permits expiry release before any submission. An authorized expiry records HOLD_EXPIRED and unrecovered exposure/loss. A submitted pending/unknown transaction, or unknown merchant payout, retains the wallet lock beyond TTL until its scripted result is reconciled. Deposits and restart cannot unlock it. The manual reset is protected and refuses unresolved authorization/submitted transactions.

## Worker, outbox and restart
Worker claims short payment leases and performs adapter calls outside POS transactions. It polls PENDING/UNKNOWN authorizations, pending settlement, and RECOVERY_REQUIRED with recovery PENDING. Terminal REIMBURSED/LOSS_ABSORBED fixtures stop progressing. Leases normally last 10 seconds; a crashed owner is retried after expiry. Graceful shutdown finishes the current bounded call. Unchanged engine versions release worker leases without duplicating events.

The mock stores events with deterministic checkout/version IDs. POS commits each newly reported event with its latest projection to the existing outbox; repeated events/versions cannot duplicate it. SSE replays to a consistent snapshot cursor, then tails SQLite with heartbeats/disconnect detection. All financial events are marked simulated. Reset preserves monotonically increasing outbox cursors and emits demo.reset.

## Existing database compatibility
Startup adds mock tables and transactionally rebuilds only the Phase 2 payments table to extend its authorization CHECK constraint with UNKNOWN. Existing rows, IDs, foreign-key references, indexes and triggers are copied and checked before commit, following the [SQLite generalized ALTER TABLE procedure](https://www.sqlite.org/lang_altertable.html#otheralter). Other new projections remain in engine-detail JSON. The SQL reference is generated from ORM metadata. Phase 1 schema detection still refuses destructive migration.

Old engine records remain readable and use their prior legacy projection path; no retroactive wallet lock, payout, or loss is invented. No blanket rewrite of historical events occurs. The default database remains backend/data/vaultpay-pos.sqlite3 when launched from backend.

## Remaining external assumptions and local access
The external adapter implements the frozen POST /v1/payments/authorize transport and GET checkout status lookup, with HMAC-SHA256 of exact bytes in X-SB-Signature. It is enabled explicitly with `PAYMENT_ENGINE_MODE=external`. Default startup uses the mock. Authorization snapshots persist payload/token and one initial submission claim. Final responses normalize to AuthorizationResult without inventing financial state. Confirm GET response/authentication/absence semantics, external financial status details, actual v5 fixtures, three-submission budget interpretation, valuation/day rules and loss/refund policy before an external rehearsal. Mock conversion is a fixed illustrative 1 USDC=1,400 KRW; it is not live pricing. Failed delivery is modeled as known, never assumed from a timeout.

The server is a trusted localhost demo; production terminal/merchant/ops authentication is not implemented. External Ops require a separate internal bearer token. Card aliases are clonable lookups, not credentials. No private keys, bank card numbers or seed phrases are stored. Reset uses a configured secret header, never a URL token. Exposing this application publicly remains outside the demo scope.

Shared contracts are frozen as vaultpay.pos.v1 in contracts/ and generated frontend/src/shared/contracts.ts. The drift checker and parallel ownership boundaries are documented in docs/parallel-development.md. This freeze implements no Phase 4–6 UI feature.
