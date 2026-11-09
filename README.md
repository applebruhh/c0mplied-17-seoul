# VaultPay

Local NFC POS hackathon demonstration. **This milestone delivers architecture and runnable scaffolding, not the complete payment application.** Only ETH, BTC and USDC are supported by the proposed contract. KRW prefunding and authorization are simulated; no real payment, wallet, asset selection or blockchain service is implemented.

## Design first
Read [architecture](docs/architecture.md), [API contract](docs/api-contract.md), [payment flow and state machines](docs/payment-flow.md), and [implementation plan](docs/implementation-plan.md). The SQLite design is in [schema.sql](backend/app/schema.sql).

## Requirements
Python 3.11+, Node 20.11+ and npm. Vite 6 and Tailwind 3 are intentionally used for the existing local Node runtime. Current Vite releases require newer Node; upgrade the toolchain together in a later change. Android platform tools/ADB and Galaxy Chrome are needed for the later NFC milestone.

## Backend
From the repository root:
```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e './backend[dev]'
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
The database is initialized at backend/data/vaultpay.sqlite3 on startup when launched from backend. Override with VAULTPAY_DATABASE_PATH. `.env.example` documents configuration; this scaffold does not automatically load `.env` files. Do not expose the local server to a shared network.

OpenAPI: http://localhost:8000/docs. Working endpoints: `/api/v1/health` and `/api/v1/capabilities`. Payment endpoints in the contract are intentionally not implemented yet. No demo customers, cards or terminal records are seeded at this milestone.

## Frontend
In a second terminal:
```sh
cd frontend
npm ci
npm run dev
```
Open http://localhost:5173/pos. Also available: `/terminal`, `/ops`, `/demo`. All four are labelled route shells; the only live frontend request is backend health. Vite proxies `/api` to port 8000, avoiding a separate CORS setup. The preview server serves built assets only; use the dev server for backend integration.

## Verification
```sh
.venv/bin/python -m pytest backend/tests
cd frontend
npm run build
```
Python dependency constraints live in pyproject.toml; the local verification snapshot is in backend/requirements-dev.lock (install after the editable project for a matching environment). npm dependencies are pinned by package-lock.json.

## Galaxy over USB (NFC implementation is a later milestone)
Enable USB debugging, connect the phone, and approve its debugging prompt:
```sh
adb devices
adb reverse tcp:5173 tcp:5173
```
Open `http://localhost:5173/terminal` in Chrome on the Galaxy. Check `window.isSecureContext` when implementing NFC; Web NFC requires a secure top-level context, compatible NDEF tags, hardware and permission. A plain LAN HTTP URL is unsuitable. Stop forwarding with `adb reverse --remove tcp:5173`.

## Adapter boundary
Both adapters implement `authorize(AuthorizationRequest)` and `get_payment(payment_id)`. The mock is an in-memory, explicitly simulated approval fixture with pending settlement, not a reservation engine. The external HTTPX adapter describes a proposed team contract and is not enabled or invoked by the scaffold. Construct its AsyncClient with an agreed trailing-slash base URL, bearer authentication and explicit timeout. Never interpret HTTP errors as financial declines.

Next: implement transactional checkout/tap services and their invariants, then SSE, Galaxy scanning, merchant flow and read-only operations. See the implementation plan for acceptance scenarios.
