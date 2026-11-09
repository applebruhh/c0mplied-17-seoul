# VaultPay architecture

## Scope and ownership
VaultPay is a local hackathon demonstration. The backend now supports checkout management, demo NFC tap processing, simulated authorization/settlement, read-only operations and durable SSE. The React application remains a Phase 1 route scaffold; it is not wired to these payment endpoints yet.

Only ETH, BTC and USDC are permitted. Stocks and tokenized securities are excluded. All KRW prefunding and merchant authorization are simulated. The Payment Engine coordinates the Embedded Wallet and company payment/settlement services. The POS records engine-reported projections and propagates status; it never performs wallet operations or financial orchestration.

| Component | Clarified responsibility |
| --- | --- |
| POS Backend | Checkouts, NFC events, Payment Engine integration, authorization status and real-time propagation |
| Payment Engine | Authorization decisions; wallet hold instructions/confirmation; company KRW capacity reservation; simulated merchant payout coordination; wallet liquidation instructions; settlement and recovery orchestration |
| Embedded Wallet | Customer crypto custody, available balances, asset holds and wallet-controlled DEX liquidation |
| Company Treasury | Receives settlement USDC; the engine reconciles reimbursement and unused reservation release |

The external sequence is wallet hold → confirm sufficient crypto → reserve company KRW capacity → simulated merchant payout → AUTHORIZED → instruct wallet liquidation → wallet DEX swap → treasury receives USDC → reconcile reimbursement and unused-hold release. POS calls only the engine's authorize/status boundary. No POS-to-Wallet, POS-to-DEX or POS-to-Treasury endpoint is added.

## Components and dependencies
Modular monolith: FastAPI/Pydantic transport; POSService for checkout lifecycle and transactional financial projections; SQLAlchemy 2.0 ORM on SQLite; adapter protocol for engine interaction; background reconciliation; persisted event outbox read by SSE. HTTPX is used for HTTP-level tests and reserved for the future external adapter. No Redis or external queue is required.

```text
frontend/                       React/Vite/TypeScript/Tailwind route shells
backend/
  app/
    main.py                     API routes, lifespan, typed responses and errors
    config.py                   Validated local settings
    models.py                   Pydantic requests, responses and state enums
    tables.py                   Authoritative SQLAlchemy schema
    schema.sql                  Generated SQL schema reference
    db.py                       Connection pragmas, transactions, demo seed data
    services.py                 Checkout/tap lifecycle, projection guards, event outbox
    streaming.py                Durable replay, snapshots and SSE tail
    worker.py                   Expiry and restart-safe engine reconciliation
    adapters/{base,mock,external}.py
  tests/                        API, constraints, races, restart, recovery and SSE tests
  pyproject.toml
  requirements-dev.lock
docs/{architecture,api-contract,payment-flow,implementation-plan}.md
```

## Database and atomicity
SQLAlchemy records use foreign keys, enum checks, bounded integer KRW amounts, and separate checkout/financial state columns. Every connection enables foreign keys, WAL and a busy timeout. Reads use explicit BEGIN for a consistent SQLite snapshot; writes use BEGIN IMMEDIATE. Transactions hold no network calls or SSE subscriptions. ORM metadata is authoritative; schema.sql is generated documentation, not a second schema initializer. See [SQLAlchemy SQLite transaction guidance](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html).

| Table | Responsibility |
| --- | --- |
| merchants / terminals | Seeded ownership and enabled terminal registration |
| customers / demo_cards | Opaque demo alias-to-customer mapping, enabled flag |
| checkouts | Immutable API amount, terminal/merchant, expiry, state, optional creation idempotency |
| payments | Unique checkout_id; unique idempotency_key constrained equal to checkout_id; financial projections and worker lease; optional merchant payout in existing engine_detail JSON |
| tap_events | Unique client_event_id, input fingerprint and correlated payment |
| events | Durable transactional outbox with AUTOINCREMENT cursor |
| mock_engine_records | Durable simulated upstream fixtures; separate from POS projections |

The partial unique checkout index permits only one WAITING_FOR_CARD or AUTHORIZING session per terminal. Payment uniqueness and the write transaction serialize competing taps across threads or processes. Accepted taps persist their fingerprint and payment association. A same-key/different-input retry is a conflict; distinct event keys for the same checkout converge on its payment. Initial authorization uses the checkout UUID, persisted before adapter submission. A lease prevents the worker from racing initial submission; unknown outcomes are reconciled by this same key.

Checkout amount, merchant and terminal cannot be modified through the API. Prefunding RESERVED confirms capacity, not payment to the merchant. New authorization requires the engine to explicitly report merchant_payout_status=COMPLETED in addition to confirmed reservations (or a valid already-completed settlement). The merchant payout projection is persisted in existing engine_detail JSON and exposed as an optional payment field; no database migration is needed. A known completed payout cannot regress. Authorization approval/decline is final. Settlement failure keeps the checkout AUTHORIZED, sets RECOVERY_REQUIRED and preserves the reported reservation. No automatic reversal, liquidation retry or blanket hold release occurs. The external engine owns recovery. Stale versions and completed-stage regressions cannot overwrite final projections.

## Background processing and persistence
An application-lifespan worker periodically expires only waiting checkouts and queries unfinished payments. It claims short persisted leases, calls the adapter outside write transactions, and commits the result plus outbox events atomically. Leases normally last 10 seconds; timeout configurations above 8 seconds increase that to timeout+2. An unchanged engine version still releases its lease so polling continues. Transient errors are retried after one second and stored as safe exception types; detailed exception text is not exposed. Graceful shutdown finishes the current bounded poll before closing connections.

Mock authorization fixtures and their start timestamps survive restarts. The mock reports wallet holds, company capacity and completed merchant payout before approval. Mock settlement reports the engine instruction to the wallet, wallet liquidation, treasury receipt, reimbursement and release, or one of two explicit recovery scenarios. A separate payout-failure fixture declines before liquidation and preserves reported holds for engine-owned compensation. This is scripted simulation, not a financial service. ExternalPaymentAdapter is a disabled placeholder. It must not be enabled until the team agrees idempotent checkout-key authorization and lookup semantics. Only an adapter's definitive EnginePaymentNotFound allows same-key resubmission; ambiguous timeouts never become declines.

SSE reads the durable outbox in batches, checks disconnects and sends heartbeats. A consistent snapshot cursor bounds reconnect replay. Snapshot and tailing have no race gap: events after the snapshot cursor are replayed by polling. All demo events are retained until an authenticated reset; AUTOINCREMENT IDs and a global reset event preserve reconnect continuity.

## Trust and local access
NFC card aliases are lookup keys, not authentication. The API resolves the customer and amount; tap bodies cannot carry an amount or outcome. An explicit checkout_id is recommended to protect against delayed taps; omitting it targets whatever checkout is active at receipt. No bank card numbers, private keys or seed phrases are stored. Every projection/event identifies simulated versus external provenance.

The server binds to localhost. This is not a production access-control system: merchant, terminal and operations endpoints have no login and are intended for a trusted local demo. Demo reset requires a configured secret header, is disabled without a secret or with an external adapter, and refuses pending authorization. Add actual merchant/terminal authentication before shared-network use. Do not implement a public financial webhook without signature validation, correlation and version checks.

The default database is `data/vaultpay-pos.sqlite3` relative to the backend working directory. Phase 1 used a different filename/schema; initialization detects that old schema and refuses destructive migration. Keep the old file and select a new path if necessary. This project has no production migration framework yet.

## Architecture clarification review
The existing two-method PaymentAdapter interface already supports the clarified external architecture: authorize submits a checkout-keyed engine request, and get_payment reads the aggregate engine projection. Method signatures, POS paths, checkout models, request bodies, existing reservation/prefund/liquidation/reimbursement enums and frontend response envelopes are preserved. The only additive response field is optional merchant_payout_status (NOT_STARTED, PENDING, COMPLETED, FAILED or null), with an additive merchant_payout.status_changed SSE event.

Phase 2 previously treated reserved company capacity as sufficient to approve. It now distinguishes capacity reservation from completed simulated merchant payout. The mock remains an aggregate engine simulation rather than implementing separate wallet/DEX services. Checkout-keyed durable mock fixtures, tap deduplication and database uniqueness remain the mechanism that prevents repeated simulated effects. The external engine must extend that same idempotency guarantee to its internal wallet holds, payouts and liquidation instructions.

Legacy engine responses without payout metadata still deserialize, and older persisted authorized records remain readable with payout unknown (null). Existing authorized records may continue settlement without inventing payout evidence or executing a retroactive payout. A **new** AUTHORIZED result without explicit payout completion is not accepted: the checkout stays AUTHORIZING and is reconciled, never silently approved or declined. A future engine integration must supply explicit payout completion for new approvals. Prior engine-detail JSON and event payloads are left untouched.

Remaining external assumptions: the engine's completed payout means confirmed simulated merchant payment, not merely an instruction accepted; AUTHORIZED certifies sufficient wallet-held crypto and reserved company capacity; versions/status lookup are authoritative; the engine deduplicates every downstream financial effect by checkout key; wallet execution and USDC treasury receipts are reflected through engine status; compensation after a failed pre-authorization payout and recovery after post-authorization liquidation/reimbursement failure belong to the engine. ExternalPaymentAdapter remains an unconfigured placeholder pending authenticated HTTP paths, response fixtures and these guarantees.
