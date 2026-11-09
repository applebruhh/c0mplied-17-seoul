# Implementation plan

## Phase 1 — completed
Architecture, initial API proposal, checkout/financial separation, schema design and React/FastAPI scaffolding. The Phase 2 contract supersedes earlier state names, paths and engine keys.

## Phase 2 — POS backend (implemented)
- [x] SQLAlchemy/SQLite models, constraints, transaction helpers and demo seed data.
- [x] Merchant/terminal ownership, create/get/active/cancel/expire checkouts.
- [x] Optional checkout-creation idempotency key with input fingerprinting.
- [x] Demo card lookup, unknown/disabled card and terminal validation.
- [x] Durable client_event_id deduplication and one payment per checkout.
- [x] Checkout UUID as engine idempotency key; serialize simultaneous taps at the database.
- [x] Separate authorization, asset reservation, prefunding, liquidation, reimbursement and settlement state.
- [x] Durable simulated adapter and explicitly disabled external placeholder.
- [x] Async settlement polling, safe uncertain outcomes, leases and restart recovery.
- [x] Recovery failure scenarios preserve authorization and reservation.
- [x] Durable SSE replay, consistent snapshots, heartbeats and reconnect cursors.
- [x] Read-only operations payment list/detail and global event stream.
- [x] Protected mock reset with no configured default secret.
- [x] API, validation, races, SQLite uniqueness, restart, settlement, SSE and reset tests.
- [x] Standalone backend startup and HTTP walkthrough documentation.

Backend financial values remain scripted simulations. ExternalPaymentAdapter makes no network calls. No real assets, DEX, ledger or AI services are implemented.

## Phase 3 — terminal and merchant UI
Implement the typed API client for `/api`, checkout amount entry, server-driven states, history, SSE reconnect handling and consecutive transactions. Add Web NFC NDEF parsing on Galaxy Chrome with permission/error handling and an explicit scan gesture. Always send checkout_id when known. Test real hardware through ADB reverse. Keep manual tap simulation visibly separate from hardware scanning.

## Phase 4 — operations frontend
Use the implemented read-only APIs for reservation, prefunding, liquidation, reimbursement and recovery. Show provenance, stage timestamps, reason codes, settlement references and preserved authorization. Add the combined presentation view. No browser financial mutation controls.

## Phase 5 — external team integration
Agree authenticated engine paths, payload fields, checkout-key idempotency, definitive not-found semantics, lookup consistency, monotonic versions and explicit recovery policy. Implement the placeholder with HTTPX only after that contract is accepted. Add contract fixtures and recovery event tests. Production authentication, real payment processing and hosted deployment remain outside the local demonstration scope.
