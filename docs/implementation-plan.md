# Implementation plan

## Milestone 0 — contract and scaffold (this change)
- [x] Inspect the empty repository.
- [x] Document architecture, ownership boundaries, state machines and payment flow.
- [x] Define public API and proposed engine adapter contract before implementation.
- [x] Design initial SQLite schema with foreign keys and uniqueness constraints.
- [x] Initialize FastAPI, Pydantic, SQLite, HTTPX and pytest project.
- [x] Define shared adapter protocol, deterministic mock response and external HTTP adapter.
- [x] Initialize React, Vite, TypeScript and Tailwind; provide four labelled route shells.
- [x] Add setup instructions and initial verification commands.

Scaffold is not a functioning payment demo. No active payment routes, NFC scanning, SSE stream, background reconciliation or financial integration is claimed at this milestone.

## Milestone 1 — backend checkout lifecycle
Implement repository/service transactions, terminal and customer/card demo seeding, checkout creation/get/cancellation/expiry and API errors. Persist creation idempotency payload hashes and response resources; reject conflicting replays. Verify constraints and concurrent active-checkout creation using independent SQLite connections.

## Milestone 2 — tap and authorization
Implement atomic tap claiming and fingerprint deduplication; server resolves amount and customer. Call adapter outside SQLite write transactions. Enforce confirmed reservations before approval; persist outcome and outbox together. Mock scenarios cover approval, decline, timeout and recovery. Verify simultaneous taps, retries with changed bodies, unknown/disabled cards, late taps, expired sessions and ambiguous timeouts.

## Milestone 3 — live merchant/terminal UI
Implement typed API client, checkout entry, history, server-driven states and consecutive purchases. Add durable SSE replay and heartbeat, snapshot cursor and reconnection handling. Add Web NFC record parsing, permission/error states, scan cancellation and duplicate suppression; explicitly label optional simulation. Test real NDEF tags on Galaxy Chrome via ADB reverse. Desktop must degrade gracefully.

## Milestone 4 — settlement projection and operations
Add restart-safe engine polling, recovery visibility, details for holds/prefund/swap/treasury/reimbursement/release and timestamps. Ops remains read-only. Verify approved authorization survives failed liquidation and holds remain until explicit engine release. Test duplicate and stale events, restart and reconnection.

## Milestone 5 — team integration and rehearsal
Agree engine paths, schema, API authentication, same-key idempotency, lookup consistency, recovery events and timeout policy with engine team. Add adapter contract fixtures from their API. External mode is blocked until these are agreed and tested. Rehearse desktop POS + Galaxy tap + operations with no wallet or blockchain implementation in this project. Record simulated versus external provenance on every financial view.

## Completion checks for the full demo
Run backend tests for the invariants above, frontend typecheck/build and Galaxy hardware checks. Confirm no client amount/outcome injection, no duplicate authorizations, no automatic post-approval reversal, and no unreported reservation releases. Keep all credentials in environment variables and all demo data local. Hosted deployment, real payment processing, AI advice, wallets, smart contracts and DEX code are outside this scope.
