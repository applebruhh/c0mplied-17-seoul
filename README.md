# VaultPay

Local NFC POS hackathon demonstration. The **POS backend is implemented and works without a frontend**. Crypto reservation, KRW prefunding, liquidation and reimbursement are simulated adapter responses. Only ETH, BTC and USDC are eligible; no actual wallet, asset selection, locking, ledger, DEX or AI service is implemented.

## Start the backend
Requires Python 3.11+. From the repository root:
```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e './backend[dev]'
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
Swagger UI: **http://localhost:8000/docs**. Health: http://localhost:8000/api/health. No frontend, NFC hardware, external credentials or other service is required.

SQLAlchemy creates `backend/data/vaultpay-pos.sqlite3` and seeds demo registrations at startup. Run commands from backend for this location; relative database paths are relative to the process working directory. Set VAULTPAY_DATABASE_PATH for another file. The Phase 1 scaffold used a different schema/file; old schemas are detected and refused rather than erased. Preserve the old file and choose a new path. No automatic destructive migration is performed.

Configuration comes from process environment; `.env.example` is a reference and is **not automatically loaded**. Optional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| VAULTPAY_DATABASE_PATH | data/vaultpay-pos.sqlite3 | SQLite file |
| VAULTPAY_DEMO_RESET_TOKEN | unset | Enable protected reset with a secret header |
| VAULTPAY_WORKER_INTERVAL | 0.25 | Reconciliation/expiry polling seconds |
| VAULTPAY_MOCK_STAGE_SECONDS | 1 | Simulated settlement stage duration |
| VAULTPAY_ENGINE_TIMEOUT | 3 | Adapter call deadline in seconds |

Keep this unauthenticated demonstration server on localhost. Card aliases are clonable demo identifiers, not credentials. The protected reset header does not add authentication to merchant or operations routes.

## Try a payment through HTTP
The seeded merchant is `merchant-demo`; registered terminals are `galaxy-01` and `galaxy-02`.

Create a checkout:
```sh
curl -sS http://localhost:8000/api/checkouts \
  -H 'Content-Type: application/json' \
  -d '{"merchant_id":"merchant-demo","terminal_id":"galaxy-01","amount_krw":4500}'
```
Copy the returned checkout_id and use it in the next request. Generate a fresh UUID for each logical NFC event (for example, `python -c 'import uuid; print(uuid.uuid4())'`). Retry the same logical event with the same UUID and body.
```sh
curl -sS http://localhost:8000/api/nfc/tap \
  -H 'Content-Type: application/json' \
  -d '{"terminal_id":"galaxy-01","checkout_id":"<checkout UUID>","card_alias":"demo-alice","client_event_id":"<fresh event UUID>"}'
```
Normal approval requires the engine to confirm the wallet hold, company capacity and completed simulated merchant payout; it returns HTTP 200 and checkout AUTHORIZED. Its separate settlement projection progresses in the background. A transport error would return 202/AUTHORIZING and be reconciled with the same checkout key. To locate the active checkout, GET `/api/terminals/galaxy-01/active-checkout`; it becomes null once authorization is definitive. GET `/api/checkouts/<checkout UUID>` retains the complete latest checkout/payment state. A new checkout can then be created on that terminal.

Inspect operations and listen for updates:
```sh
curl -sS http://localhost:8000/api/ops/payments
curl -N http://localhost:8000/api/ops/events
```
For checkout events use `/api/checkouts/<checkout UUID>/events`. For a finite SSE response in Swagger/curl, add `?follow=false`. To replay persisted events and get a fresh snapshot, use `?after=0&follow=false` or the Last-Event-ID header. Disconnect and reconnect with the last cursor; no in-memory subscription state is needed. Ops snapshots contain the newest 100 payments and a total; use the paginated list for older rows.

Use a fresh checkout for each scenario:

| card_alias | Simulated behavior |
| --- | --- |
| demo-alice | Approved, settles and releases reservation in approximately three seconds |
| demo-declined | Declined with DEMO_SPENDING_LIMIT |
| demo-payout-fail | Merchant payout fails; declined before liquidation with reported hold preserved |
| demo-liquidation-fail | Approved, then RECOVERY_REQUIRED; reservation stays RESERVED |
| demo-reimbursement-fail | Approved, then reimbursement fails; recovery with reservation preserved |

Create a checkout with `expires_in_seconds: 1` and wait to try expiry. Cancel a waiting checkout with POST `/api/checkouts/<checkout UUID>/cancel`. AUTHORIZING sessions cannot be cancelled or expired because the financial outcome is unresolved.

## Protected demo reset
Set a secret before launching the backend, for example `export VAULTPAY_DEMO_RESET_TOKEN='choose-a-local-secret'`. In the same shell where the variable is available:
```sh
curl -sS -X POST http://localhost:8000/api/demo/reset \
  -H "X-Demo-Reset-Token: $VAULTPAY_DEMO_RESET_TOKEN"
```
Without a configured token reset is disabled (503); wrong/missing header returns 403. Reset deletes simulated checkout/payment/tap/mock-engine history and appends a reset event, keeping terminal/customer/card registrations and monotonic event IDs. Unresolved authorization blocks reset (409). Never use reset to reverse an authorization.

## Tests and dependencies
```sh
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m pip check
```
Tests use temporary file-backed SQLite databases and ASGI HTTP requests; no backend process or frontend is needed. They exercise validation, creation/idempotency, terminal/card mapping, duplicates, races across app instances, expiry/cancellation, uncertain outcomes, restart recovery, settlement failures, SSE replay/live delivery and protected reset.

Dependency constraints are in backend/pyproject.toml. backend/requirements-dev.lock records the verified Python 3.11 environment; for matching versions install with `python -m pip install -r backend/requirements-dev.lock -e './backend[dev]'` from the root. This snapshot pins versions without distribution hashes.

## Architecture and adapter boundary
Read [architecture](docs/architecture.md), [API contract](docs/api-contract.md), [state machines](docs/payment-flow.md), and [implementation plan](docs/implementation-plan.md). SQLAlchemy models in backend/app/tables.py are the authoritative schema; schema.sql is a generated reference.

Both adapters implement async `authorize(AuthorizationRequest)` and `get_payment(checkout_id)`. **The checkout UUID is the payment idempotency key.** MockPaymentAdapter persists scripted upstream fixtures separately from POS state so a restart can reconcile unfinished payments. ExternalPaymentAdapter is a placeholder that raises NotImplementedError and makes no network calls. A future HTTPX implementation must agree authenticated paths, same-key lookup/idempotency and recovery/version semantics with the engine team. No environment flag enables live financial calls. Embedded Wallet custody, holds and wallet-controlled DEX execution are internal dependencies of the external Payment Engine, never direct POS integrations. Optional merchant_payout_status distinguishes reserved company capacity from merchant payment and is stored in existing engine-detail JSON; no database migration is required. Legacy payout metadata remains unknown rather than inferred, and new approvals require explicit payout completion.

## Frontend (still Phase 1 shells)
Requires Node 20.11+ and npm; Vite 6/Tailwind 3 support this local runtime.
```sh
cd frontend
npm ci
npm run dev
```
Routes `/pos`, `/terminal`, `/ops`, `/demo` currently show labelled shells and backend health only. The new payment APIs are ready for the next UI phase. Vite proxies `/api` to localhost:8000. Frontend build: `npm run build`.

For future Galaxy hardware testing enable USB debugging, connect/approve the phone and run `adb reverse tcp:5173 tcp:5173`. Open `http://localhost:5173/terminal` in Galaxy Chrome. Web NFC scanning is not implemented in the shell yet; it will require NFC hardware, an NDEF demo tag, a user gesture and a top-level secure context. Remove forwarding with `adb reverse --remove tcp:5173`.
