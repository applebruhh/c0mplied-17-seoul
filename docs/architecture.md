# VaultPay architecture

## Scope and delivery boundary
VaultPay is a local hackathon demonstration for crypto-funded everyday purchases. This milestone defines the system and initializes runnable scaffolding. Payment orchestration, SSE delivery, NFC scanning, and operational screens are subsequent implementation steps; route shells must not imply those features already work.

Only ETH, BTC, and USDC are eligible. Stocks and tokenized securities are excluded. KRW prefunding and merchant authorization are simulated. The external team owns identity/limits, selection, wallets, holds, risk, DEX swaps, treasury, and financial ledgers. POS never independently reserves or liquidates assets.

## Components
A modular monolith consists of a React/Vite/TypeScript/Tailwind frontend and a FastAPI/Pydantic backend using SQLite and HTTPX. One backend process is sufficient for the demo. Galaxy Chrome and desktop browsers use the same API through the Vite `/api` proxy. No Redis, queue service, or blockchain SDK is needed.

- `/pos`: merchant checkout creation, server-driven status and history.
- `/terminal`: NDEF card identification, explicit scan gesture and server-driven result.
- `/ops`: read-only transaction timeline and recovery information.
- `/demo`: presentation combining merchant and operations information.
- Backend modules: API transport, checkout service, payment orchestration, event persistence, repositories, adapter boundary. These can begin in a small number of files and split when implemented.
- `PaymentAdapter`: identical typed authorize and status methods for mock and external engines. Mock results are explicitly simulated. External results remain tagged external even if upstream reports a sandbox.

## Trust and financial boundaries
The merchant sets an integer KRW amount when creating a checkout; it becomes immutable once waiting. A tap contains checkout identity and demo card ID, never an amount, customer balance, or approval. Backend resolves a seeded opaque demo card ID to a customer and checks terminal ownership, current checkout, expiry, state and idempotency. NFC identifiers are clonable demo lookup keys, never authentication. Use NDEF text `vaultpay:card:demo-alice`, not a hardware serial or a bank-card PAN.

The engine is the only authority for financial outcomes. The backend stores its reported projections rather than implementing a ledger. Authorization is immutable once approved. Settlement failure sets recovery required without reversing authorization or releasing the hold. A transport timeout is an unknown result, not a decline; reconcile by the original engine idempotency key before proceeding.

This scaffold is localhost-only and has no production authentication. Before shared-network use, add merchant/terminal pairing, authorization and authenticated engine callbacks. Ops is read-only by API design, not an access-control system. Never store private keys, seed phrases, real card numbers or bearer secrets in events/logs.

## State machines
Checkout and financial state are independent. See payment-flow.md for diagrams and transition guards. Checkout: WAITING → PROCESSING → APPROVED | DECLINED; WAITING → CANCELLED | EXPIRED. An uncertain authorization keeps PROCESSING, blocking another attempt until reconciliation. An approved checkout can coexist with SETTLEMENT_PENDING or RECOVERY_REQUIRED.

Financial projection: authorization PENDING → AUTHORIZED | DECLINED (UNKNOWN is reconciled to a definitive result); settlement NOT_STARTED → PENDING → SETTLED | RECOVERY_REQUIRED. RECOVERY_REQUIRED → PENDING only following an explicit upstream recovery event. Asset hold and KRW reservation each track NONE, RESERVED, PARTIALLY_RELEASED, RELEASED or UNKNOWN separately. A release is recorded only from an explicit engine event; POS never infers one from a failure.

## Persistence and concurrency
`backend/app/schema.sql` is the initial SQLite schema. UTC ISO-8601 timestamps, UUID application IDs, integer KRW amounts, decimal strings for crypto quantities. Tables:

| Table | Responsibility and constraints |
| --- | --- |
| customers | Demo customer lookup only |
| demo_cards | Unique opaque identifier and enabled flag |
| terminals | Demo terminal identity |
| checkouts | Immutable amount, expiry, unique active session per terminal |
| payments | One payment per checkout; engine key; separate authorization, settlement and reservation projections |
| tap_requests | Persist request key and payload fingerprint, link to the resulting payment |
| engine_events | Unique upstream event ID for callback deduplication |
| events | Ordered durable SSE outbox with a monotonically increasing integer cursor |

Use foreign keys, WAL and a busy timeout on every connection. In a short BEGIN IMMEDIATE transaction, resolve the active checkout, insert/deduplicate tap request, claim WAITING → PROCESSING, create the payment and append an outbox event. Commit before HTTP calls. The upstream idempotency key is the payment UUID and is persisted before the call. Record the result and its outbox events atomically. Unique indexes provide correctness across racing taps; an in-memory lock is insufficient. The scaffold initializes the schema; orchestration transactions are planned.

Recover PROCESSING payments on startup by querying the engine by the persisted key. An external engine must support idempotent authorize and lookup by key to resolve a timeout/crash before its response was persisted. Do not enable external mode until the team agrees on that contract. Persist webhook ingestion and sequence/version checks before updating projections. Reject amount/currency/payment correlation mismatches. Stale events cannot regress finalized authorization or settlement.

## Runtime and failure handling
Polling reconciliation runs independently of the customer request; it survives restarts by selecting unfinished payments from SQLite. Initial demo can poll instead of exposing an unauthenticated callback. SSE reads the durable outbox, sends heartbeat comments and replays after Last-Event-ID; clients fetch a fresh snapshot on initial connection and after replay gaps. Avoid a transaction held open during streaming.

## Proposed tree
```text
backend/
  app/{main.py,models.py,db.py,schema.sql}
  app/adapters/{base.py,mock.py,external.py}
  tests/
  pyproject.toml
frontend/
  src/{App.tsx,main.tsx,styles.css,vite-env.d.ts}
  package.json
  vite.config.ts
  tsconfig.json
docs/{architecture.md,api-contract.md,payment-flow.md,implementation-plan.md}
README.md
.env.example
```
Later add backend services/repositories, frontend API/event/NFC hooks and screen components at the steps in the implementation plan.
