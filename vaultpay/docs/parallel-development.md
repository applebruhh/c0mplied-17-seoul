# Phase 4–6 parallel handoff

Phase 3 financial simulation is complete. This task prepares contracts only; the UI phases below are not implemented by this change. For this repository handoff, the parallel work lanes are assigned as follows.

| Lane | Owns | Uses frozen contracts for |
| --- | --- | --- |
| Phase 4 — Galaxy terminal | frontend/src/features/terminal/ and /terminal wiring | Web NFC permission/scan lifecycle; resolve demo alias or normalize registered UID to card_alias; active checkout lookup; stable client_event_id per logical tap; tap/status/SSE UI |
| Phase 5 — Merchant POS | frontend/src/features/pos/ and /pos wiring | Merchant/terminal selection; integer KRW checkout creation; cancel/expiry/status; history; consecutive purchases; snapshot/SSE reconnect |
| Phase 6 — Operations/presentation | frontend/src/features/ops/, frontend/src/features/demo/, /ops and /demo wiring; integration QA fixtures | Read-only history/detail/timelines; exposure/hold/unknown/recovery views; simulated/external provenance; composed presentation and agreed end-to-end rehearsal |

Each lane owns its feature-local components/hooks/tests. These paths are reserved ownership boundaries, not newly scaffolded features. Phase numbers here allocate local work; actual financial Engine/Embedded Wallet/Treasury execution remains the external team's responsibility.

## Shared owner / coordinated changes only
- backend/app/models.py, contracts.py, adapters/base.py, signing.py and external.py.
- Existing POS route signatures, services, worker, persistence schema/migrations and authorization snapshot semantics.
- frontend/src/shared/contracts.ts and all contracts/ generated artifacts; scripts/freeze_contracts.py owns regeneration.
- Frontend route registration, shared navigation/styles and dependency manifests: agree integration changes before overlapping edits. Feature-local work should not rewrite shared App.tsx independently.

No UI lane should modify Phase 3 wallet locking, router fixtures, retries, slippage, daily caps, loss absorption or migrations as part of screen development. Request a shared-owner contract change rather than implementing a new financial interpretation.

## Frozen integration rules
- All consumers import shared TypeScript types. Pydantic/OpenAPI are authoritative; run the drift checker before handoff.
- Public POS endpoints and checkout/tap bodies remain unchanged. frontend never chooses authorization outcome or replaces the backend checkout amount.
- Preserve client_event_id for retries; send known checkout_id to bind delayed taps. Do not generate a fresh checkout after a timeout.
- Keep POS payment_id (local UUID) distinct from engine_payment_id. History/detail routes take the local ID.
- Only AUTHORIZED/DECLINED are definitive purchase outcomes. UNKNOWN/PENDING map to AUTHORIZING. An authorized purchase can still have recovery or an unknown transaction.
- financial_status_reported=false means existing initial reservation/prefund/settlement values are not engine-confirmed details. Show “Not reported by engine”; do not infer locks, payouts, swaps, receipts or reimbursement from approval alone.
- Source simulated/external is mandatory provenance. External transport implementation does not enable real financial calls by default; the app still uses MockPaymentAdapter unless explicitly injected.
- Snapshot first, then live events; reconnect with Last-Event-ID, apply snapshots and deduplicate durable event IDs. Heartbeats are comments, not state changes. Reset clears client caches.
- HMAC and SB_POS_HMAC_SECRET stay backend-only. No VITE_ secret, browser signing, direct engine/wallet/DEX calls, or credentials in events/URLs.

## Card/merchant fixtures
Existing merchant-demo / galaxy-01 / galaxy-02 demo cards remain. Integration fixture merchant=cafe, terminal=cafe-01 and registered alias=04A1B2C3D4E5F6 share the seeded success customer for local tests. Terminal should normalize UID separators/whitespace before passing the registered alias; the backend hashes its uppercase ASCII hex representation into card_token. A raw UID with colons does not match the existing card_alias request pattern. Unregistered UIDs still receive UNKNOWN_CARD locally; UNKNOWN_WALLET is a distinct engine decline.

The SHA-256 token is a deterministic demo lookup token, not secure card authentication. Multiple NFC events for the same checkout cannot replace the initially selected token. Exact authorization context is saved before calling the adapter, and the worker reads that snapshot after a restart rather than recomputing identity or payload.

## Acceptance per lane
Run contract drift/type checks and relevant feature tests; preserve backend pytest success. Galaxy hardware testing belongs to Phase 4. Merchant consecutive-checkout interaction belongs to Phase 5. Read-only recovery/provenance and composed rehearsal belong to Phase 6. Do not activate the external adapter or contact a live engine as part of routine UI tests; use the mock or HTTPX contract fixtures.

Before external rehearsal, the shared integration owner must obtain an actual GET status response/error fixture and confirm GET signature rules/404 absence semantics. The supplied authorize response has no financial detail schema; Phase 6 must not invent one to fill its dashboard.
