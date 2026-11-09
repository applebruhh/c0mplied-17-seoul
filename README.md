# BlockBucks

Run the BlockBucks commands below from the [`vaultpay/` directory](vaultpay/).

Local NFC POS hackathon demonstration. The **POS backend is implemented and works without a frontend**. Crypto reservation, KRW prefunding, liquidation and reimbursement are simulated adapter responses. Only deposited BTC, ETH, USDC and USDT wallet assets are eligible; no actual wallet, asset selection, locking, ledger, DEX or AI service is implemented.

## BlockBucks 화면 (가게 · 단말기 · 고객 앱 · 운영)

Figma "StockBucks — Complete 16+1 Screen UI Suite"를 바탕으로 만든 최종 UI입니다. 프런트엔드를 빌드하면 FastAPI가 8000번 포트에서 모든 화면을 함께 서빙합니다.

| 기기 | 주소 | 화면 |
| --- | --- | --- |
| 가게 노트북 | `http://127.0.0.1:8000/pos` | 금액 키패드 → 결제 요청 → 카드 대기 → 승인/거절을 크게 표시 (승인 결과만) |
| 갤럭시 (USB) | `http://localhost:8000/terminal` | Web NFC 카드 리더. 결제 태그 + 고객 요청 시 **카드 등록 모드** |
| 고객 휴대폰 | `http://localhost:8000/app` | 가입(이름·결제 코인·매도 성향) → 필수 동의 2개 → 지갑 생성 → 카드 등록 → 홈·충전·출금·매도 미리보기·결제/차감 내역 |
| 운영자 | `http://127.0.0.1:8000/ops` | KPI, 결제 4개 상태 축, 스왑 내역, 이벤트 타임라인, 고객 지갑·잠금, 단말기 |
| 안내 | `http://127.0.0.1:8000/` | 화면 링크와 기기 연결 순서 |

### 윈도우에서 한 번에 실행
```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-demo.ps1                       # mock 엔진
powershell -ExecutionPolicy Bypass -File scripts\start-demo.ps1 -External `
  -EngineUrl https://<엔진 주소> -HmacSecret <테스트 HMAC 키>                           # 테스트 엔진
```
처음 실행 시 `.venv`와 `node_modules`를 설치하고, 프런트엔드를 빌드하고, USB로 연결된 **모든** 안드로이드 기기에 `adb reverse tcp:8000 tcp:8000`을 건 뒤 서버를 띄웁니다. 갤럭시와 고객 폰을 둘 다 USB로 꽂으면 두 폰 모두 `localhost:8000`으로 접속합니다. 고객 폰을 Wi-Fi로 붙이려면 `-Lan`을 더하고 `http://<노트북 IP>:8000/app`을 엽니다(인증 없는 데모 서버가 같은 네트워크에 열리므로 신뢰할 수 있는 네트워크에서만 쓰세요).

### 실물 카드로 결제하기
1. 고객 앱에서 가입 → (mock이면) 100 USDC 데모 충전 → **카드 등록 → 매장 단말기 → 등록 요청 보내기**.
2. 갤럭시 화면이 "OO님의 카드를 등록해요"로 바뀌면 **등록용 NFC 스캔 시작**을 누르고 카드를 폰 뒷면에 댑니다. UID가 그 고객에게 묶이고 고객 앱이 홈으로 넘어갑니다. (고객 폰에서 직접 스캔하거나 UID를 입력해도 됩니다.)
3. 노트북 POS에서 금액(예: 4,500원) 입력 → **결제 요청** → 갤럭시에서 **NFC 스캔 시작** → 카드 태그.
4. POS에 승인/거절이 뜨고, 고객 앱에 "결제 완료" 알림과 함께 지갑 잠금 → 코인 매도 → 차액 USDC 환급이 차례로 반영됩니다. 운영 콘솔에서 스왑 내역과 이벤트를 확인합니다.

등록되지 않은 카드를 태그하면 갤럭시 화면에 UID가 크게 나옵니다. 그 UID를 고객 앱 "UID 입력"에 넣어도 됩니다.

**외부 엔진 모드**에서는 지갑 잔고·충전·출금이 엔진 소관이라 고객 앱은 가입, 카드 등록(로컬 바인딩 + 카드 토큰 표시), 결제 내역만 다룹니다. 표시된 카드 토큰은 엔진 담당자가 `POST /v1/cards`로 테스트 지갑에 등록해야 결제됩니다. 외부 모드의 운영 콘솔은 [로컬 Ops 리스너](#local-only-operations-demo)로 엽니다.

새 API(`/api/app/*`, `/api/terminals/{id}/pending-enrollment`, `/api/terminals/{id}/enrollments/{id}/complete`, `/api/ops/overview`)는 Swagger(`/docs`)에서 볼 수 있습니다. 고객 식별은 폰에 저장된 고객 ID뿐(비밀번호 없음)이고, mock 모드의 코인 수량은 고정 데모 시세로 환산한 값입니다.

## Start the backend
Requires Python 3.11+. From `vaultpay/`:
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

Bind the backend to localhost or a private upstream network. Mock mode supports local Ops without a token by default. External mode requires a separate Ops bearer and disables Ops when the token is unset. Card aliases are clonable demo identifiers. See [internal gateway setup](vaultpay/docs/phase7a-engine.md#internal-operations-gateway) for browser access.

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

Dependency constraints are in backend/pyproject.toml. backend/requirements-dev.lock records the verified Python 3.11 environment; for matching versions install with `python -m pip install -r backend/requirements-dev.lock -e './backend[dev]'` from `vaultpay/`. This snapshot pins versions without distribution hashes.

## Architecture and adapter boundary
Read [architecture](vaultpay/docs/architecture.md), [API contract](vaultpay/docs/api-contract.md), [state machines](vaultpay/docs/payment-flow.md), and [implementation plan](vaultpay/docs/implementation-plan.md). SQLAlchemy models in backend/app/tables.py are the authoritative schema; schema.sql is a generated reference.

Both adapters implement async `authorize(AuthorizationRequest)` and `get_payment(checkout_id)`. The checkout UUID is the payment idempotency key. Mock authorization and settlement use durable local fixtures. External mode signs immutable authorization bytes and saves verified decisions. On restart the worker validates and applies a stored terminal decision before any status lookup. An uncertain submission without a final snapshot stays `UNKNOWN` while the worker looks up the original checkout ID.

The external adapter reports authorization separately from settlement. Embedded Wallet custody, holds and wallet-controlled liquidation are responsibilities of the external engine. See [Phase 7A setup and Phase 7B checks](vaultpay/docs/phase7a-engine.md) for the HTTPS engine origin, test credentials, card enrollment and upstream contract gaps.

## Merchant POS demo
Requires Node 20.11+ and npm; Vite 6/Tailwind 3 support this local runtime.
```sh
cd frontend
npm ci
npm run dev
```
With the backend running, open [Merchant POS](http://localhost:5173/pos). Enter a whole KRW amount (keypad or presets) and select **결제 요청**. The page uses the seeded merchant `cafe` and terminal `cafe-01`. It displays the checkout ID and amount returned by the backend.

To simulate a tap, paste the displayed checkout ID into this command.

```sh
curl -sS http://localhost:8000/api/nfc/tap \
  -H 'Content-Type: application/json' \
  -d '{"terminal_id":"cafe-01","checkout_id":"<checkout UUID>","card_alias":"04A1B2C3D4E5F6","client_event_id":"<fresh event UUID>"}'
```

Use `demo-declined` to try a decline or `demo-authorization-unknown` to see an unresolved result reconcile. The page polls checkout status and restores the tracked checkout on refresh in the same tab. If checkout creation could not be confirmed, **같은 주문 다시 보내기** resends the same amount and idempotency key. Select **다음 주문** once the backend reports a final status.

Vite proxies `/api` to localhost:8000. Run `npm run build` from `frontend` to typecheck and build.

## Galaxy NFC terminal

The built terminal runs at `http://localhost:8000/terminal` alongside the backend. See the [Galaxy setup, demo tag registration, and hardware checklist](vaultpay/docs/galaxy-terminal.md).

## Local-only Operations demo

Build with `npm --prefix frontend run build`. Export `DEMO_LOCAL_OPS=true` and a random `DEMO_LOCAL_OPS_ACCESS_CODE` of at least 24 characters. From `backend/`, run `../.venv/bin/python -m app.demo`. Open `http://127.0.0.1:8000/ops` and enter the code. This listener also serves `/pos` and `/terminal`. The session expires after 15 minutes. Use a direct loopback connection and keep this listener off tunnels and proxies.

Set `PAYMENT_ENGINE_MODE=mock` for an offline demo. For an authorized external rehearsal, set `PAYMENT_ENGINE_MODE=external`, `PAYMENT_ENGINE_BASE_URL`, `SB_POS_HMAC_SECRET` and a separate `VAULTPAY_DATABASE_PATH`. Set `VAULTPAY_OPS_TOKEN` for bearer-authenticated raw engine Ops calls. The local browser session grants read-only access to the local Ops list and detail.

## Shared contract freeze for Phase 4–6
Contract version: **vaultpay.pos.v1**. Import generated TypeScript types from frontend/src/shared/contracts.ts; Pydantic schemas are authoritative. Review [shared contracts](vaultpay/contracts/README.md), [API contract](vaultpay/docs/api-contract.md) and [parallel ownership](vaultpay/docs/parallel-development.md).
```sh
.venv/bin/python scripts/freeze_contracts.py --check
npm --prefix frontend run typecheck
```
The normal uvicorn command defaults to mock mode. External mode requires a server-side HMAC key and engine origin.

The mock integration fixture uses merchant `cafe`, terminal `cafe-01` and card alias `04A1B2C3D4E5F6`. External mode requires explicit enrollment of the test card and derives its SHA-256 ASCII UID token from the registered UID. The public NFC request fields are unchanged. External authorization responses use `financial_status_reported=false`. The Operations screen displays unreported financial detail accordingly.
