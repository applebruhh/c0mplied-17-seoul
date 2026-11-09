# VaultPay

Local NFC POS hackathon demonstration. The **POS backend is implemented and works without a frontend**. Crypto reservation, KRW prefunding, liquidation and reimbursement are simulated adapter responses. Only deposited BTC, ETH, USDC and USDT wallet assets are eligible; no actual wallet, asset selection, locking, ledger, DEX or AI service is implemented.

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

SQLAlchemy creates `backend/data/vaultpay-pos.sqlite3` and seeds demo registrations at startup. Run commands from backend for this location; relative database paths are relative to the process working directory. Set VAULTPAY_DATABASE_PATH for another file. The Phase 1 scaffold used a different schema/file; Phase 1 schemas are detected and refused rather than erased. Phase 2 databases are upgraded in place to allow authorization UNKNOWN while retaining existing payments, tap IDs, events and indexes; new mock tables are additive.

Configuration comes from process environment; `.env.example` is a reference and is **not automatically loaded**. Optional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| PAYMENT_ENGINE_MODE | mock | Set to `external` to use Stockbucks |
| PAYMENT_ENGINE_BASE_URL | unset | Engine origin, required in external mode |
| SB_POS_HMAC_SECRET | unset | Backend-only signing key, required in external mode |
| VAULTPAY_OPS_TOKEN | unset | Backend bearer token for internal Operations |
| VAULTPAY_DATABASE_PATH | data/vaultpay-pos.sqlite3 | SQLite file |
| VAULTPAY_DEMO_RESET_TOKEN | unset | Enable protected reset with a secret header |
| VAULTPAY_WORKER_INTERVAL | 0.25 | Reconciliation/expiry polling seconds |
| VAULTPAY_MOCK_STAGE_SECONDS | 1 | Simulated settlement stage duration |
| VAULTPAY_ENGINE_TIMEOUT | 3 | Adapter call deadline in seconds |
| VAULTPAY_MOCK_MARGIN | 0.03 | Whole-wallet authorization buffer |
| VAULTPAY_MOCK_MAX_SLIPPAGE | 0.005 | Maximum submitted slippage (cannot exceed 0.5%) |
| VAULTPAY_MOCK_HOLD_TTL | 300 | Hold deadline; set 60 for demonstrations |
| VAULTPAY_MOCK_UNKNOWN_SECONDS | 5 | Scripted unknown-outcome reconciliation delay |

Bind the backend to localhost or a private upstream network. Mock mode supports local Ops without a token by default. External mode requires a separate Ops bearer and disables Ops when the token is unset. Card aliases are clonable demo identifiers. See [internal gateway setup](docs/phase7a-engine.md#internal-operations-gateway) for browser access.

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
Normal approval requires the engine to confirm the wallet hold, company capacity and completed simulated merchant payout; it returns HTTP 200 and checkout AUTHORIZED. Its separate settlement projection progresses in the background. A transport timeout or unknown decision returns 202/AUTHORIZING with financial UNKNOWN and is reconciled with the same checkout key. To locate the active checkout, GET `/api/terminals/galaxy-01/active-checkout`; it becomes null once authorization is definitive. GET `/api/checkouts/<checkout UUID>` retains the complete latest checkout/payment state. A new checkout can then be created on that terminal. The same customer wallet remains locked until settlement or reconciled loss; a new payment against that wallet receives WALLET_BUSY while it is locked.

Inspect operations and listen for updates:
```sh
curl -sS http://localhost:8000/api/ops/payments
curl -N http://localhost:8000/api/ops/events
```
For checkout events use `/api/checkouts/<checkout UUID>/events`. For a finite SSE response in Swagger/curl, add `?follow=false`. To replay persisted events and get a fresh snapshot, use `?after=0&follow=false` or the Last-Event-ID header. Disconnect and reconnect with the last cursor; no in-memory subscription state is needed. Ops snapshots contain the newest 100 payments and a total; use the paginated list for older rows.

Use a fresh checkout for each scenario:

| card_alias | Simulated behavior |
| --- | --- |
| demo-alice | Whole-wallet hold, BTC-first route, reimbursement, excess USDC return and release |
| demo-declined | Insufficient total prepaid value: LIMIT_EXCEEDED |
| demo-wallet-busy | Prelocked fixture: WALLET_BUSY |
| demo-execution-fail | Sufficient total balance but insufficient executable value under caps |
| demo-prefund-fail | Company capacity unavailable; lock released |
| demo-payout-fail | Definitive payout failure; decline and explicit wallet/capacity cleanup |
| demo-hold-expiry | Approval but no submission until hold expires; exposure/loss recorded and wallet expires |
| demo-slippage-fail | Three rejected submissions, then reconciled LOSS_ABSORBED |
| demo-alternate | BTC route fails, ETH fallback succeeds |
| demo-liquidation-fail | Fallback/retry exhaustion, exposure, then LOSS_ABSORBED and release |
| demo-reimbursement-fail | Known failed treasury delivery, loss absorption and release |
| demo-unknown | Submitted transaction UNKNOWN; wallet stays locked until scripted confirmation |
| demo-authorization-unknown | Authorization UNKNOWN before a later confirmed payout decision |
| demo-usdt | USDT-only prepaid wallet, no stablecoin percentage cap |

The default demo wallet starts with 30,000 BTC-value KRW, 40,000 ETH-value KRW, 20,000 USDC-value KRW and 10,000 USDT-value KRW. These are synthetic valuation snapshots, not crypto custody or live quotes. Authorization requires ceil(amount × 1.03). Non-stablecoin sells cap at 30% per asset/day; fixed fixture ranking does not force stablecoins first. Three total submissions **including retries** share one payment budget, with at most two retries per swap. Submitted slippage cannot exceed 0.5%.

For a short hold-expiry demo start with `export VAULTPAY_MOCK_HOLD_TTL=60`. Pending/unknown submitted transactions remain locked past the deadline until reconciliation. On a known exhausted failure, the company explicitly absorbs the unpaid merchant exposure before releasing the service hold; AUTHORIZED never changes to FAILED. Historical settlement RECOVERY_REQUIRED plus recovery LOSS_ABSORBED denotes that reconciled loss. See the API/state docs for exact policy assumptions.

Create a checkout with `expires_in_seconds: 1` and wait to try expiry. Cancel a waiting checkout with POST `/api/checkouts/<checkout UUID>/cancel`. AUTHORIZING sessions cannot be cancelled or expired because the financial outcome is unresolved.

## Protected demo reset
Set a secret before launching the backend, for example `export VAULTPAY_DEMO_RESET_TOKEN='choose-a-local-secret'`. In the same shell where the variable is available:
```sh
curl -sS -X POST http://localhost:8000/api/demo/reset \
  -H "X-Demo-Reset-Token: $VAULTPAY_DEMO_RESET_TOKEN"
```
Without a configured token reset is disabled (503); wrong/missing header returns 403. Reset deletes simulated checkout/payment/tap/mock-engine history and appends a reset event, keeping terminal/customer/card registrations and monotonic event IDs. Unresolved authorization or pending/unknown submitted swaps block reset (409). Reset also restores mock wallet snapshots and company capacity. Never use reset to reverse an authorization.

## Tests and dependencies
```sh
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m pip check
```
Tests use temporary file-backed SQLite databases and ASGI HTTP requests; no backend process or frontend is needed. They exercise validation, creation/idempotency, terminal/card mapping, duplicates, races across app instances, expiry/cancellation, uncertain outcomes, restart recovery, whole-wallet locking, prepaid snapshots, route/retry budgets, expiry, loss absorption, SSE replay/live delivery and protected reset.

Dependency constraints are in backend/pyproject.toml. backend/requirements-dev.lock records the verified Python 3.11 environment; for matching versions install with `python -m pip install -r backend/requirements-dev.lock -e './backend[dev]'` from the root. This snapshot pins versions without distribution hashes.

## Architecture and adapter boundary
Read [architecture](docs/architecture.md), [API contract](docs/api-contract.md), [state machines](docs/payment-flow.md), and [implementation plan](docs/implementation-plan.md). SQLAlchemy models in backend/app/tables.py are the authoritative schema; schema.sql is a generated reference.

Both adapters implement async `authorize(AuthorizationRequest)` and `get_payment(checkout_id)`. The checkout UUID is the payment idempotency key. Mock authorization and settlement use durable local fixtures. External mode signs immutable authorization bytes and saves verified decisions. On restart the worker validates and applies a stored terminal decision before any status lookup. An uncertain submission without a final snapshot stays `UNKNOWN` while the worker looks up the original checkout ID.

The external adapter reports authorization separately from settlement. Embedded Wallet custody, holds and wallet-controlled liquidation are responsibilities of the external engine. See [Phase 7A setup and Phase 7B checks](docs/phase7a-engine.md) for the HTTPS engine origin, test credentials, card enrollment and upstream contract gaps.

## Merchant POS demo
Requires Node 20.11+ and npm; Vite 6/Tailwind 3 support this local runtime.
```sh
cd frontend
npm ci
npm run dev
```
With the backend running, open [Merchant POS](http://localhost:5173/pos). Enter a whole KRW amount and select **Create Checkout**. The page uses the seeded merchant `cafe` and terminal `cafe-01`. It displays the checkout ID and amount returned by the backend.

To simulate a tap, paste the displayed checkout ID into this command.

```sh
curl -sS http://localhost:8000/api/nfc/tap \
  -H 'Content-Type: application/json' \
  -d '{"terminal_id":"cafe-01","checkout_id":"<checkout UUID>","card_alias":"04A1B2C3D4E5F6","client_event_id":"<fresh event UUID>"}'
```

Use `demo-declined` to try a decline or `demo-authorization-unknown` to see an unresolved result reconcile. The page polls checkout status and restores the tracked checkout on refresh in the same tab. If checkout creation could not be confirmed, **Retry Checkout Creation** resends the same amount and idempotency key. Select **Next Checkout** once the backend reports a final status.

Vite proxies `/api` to localhost:8000. Run `npm run build` from `frontend` to typecheck and build.

## Galaxy NFC terminal

The built terminal runs at `http://localhost:8000/terminal` alongside the backend. See the [Galaxy setup, demo tag registration, and hardware checklist](docs/galaxy-terminal.md).

## Shared contract freeze for Phase 4–6
Contract version: **vaultpay.pos.v1**. Import generated TypeScript types from frontend/src/shared/contracts.ts; Pydantic schemas are authoritative. Review [shared contracts](contracts/README.md), [API contract](docs/api-contract.md) and [parallel ownership](docs/parallel-development.md).
```sh
.venv/bin/python scripts/freeze_contracts.py --check
npm --prefix frontend run typecheck
```
The normal uvicorn command defaults to mock mode. External mode requires a server-side HMAC key and engine origin.

The mock integration fixture uses merchant `cafe`, terminal `cafe-01` and card alias `04A1B2C3D4E5F6`. External mode requires explicit enrollment of the test card and derives its SHA-256 ASCII UID token from the registered UID. The public NFC request fields are unchanged. External authorization responses use `financial_status_reported=false`. The Operations screen displays unreported financial detail accordingly.
