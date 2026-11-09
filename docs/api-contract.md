# VaultPay API contract · Phase 2

The Phase 2 request supersedes the Phase 1 paths, checkout names, financial projections and payment key. Public endpoints now use `/api`, card identifiers use `card_alias`, and the **checkout UUID is the engine idempotency key**. `/api/v1/health` and `/api/v1/capabilities` remain as compatibility aliases for the existing frontend shell. FastAPI generates executable request/response schemas at `/docs` and `/openapi.json`.

## Conventions
JSON uses snake_case. Timestamps are UTC ISO-8601 (`Z`). Amounts are strict integer KRW, 1–10,000,000; booleans, strings and fractional amounts are rejected. Unknown command fields are rejected. UUID IDs are validated. Currency is always KRW. Only ETH, BTC and USDC may appear in engine projections. NFC aliases are clonable demo lookup keys, never authentication.

Domain errors: `{ "error": { "code": "UNKNOWN_CARD", "message": "...", "request_id": "uuid" } }`. Input validation uses FastAPI's standard HTTP 422 `detail` response. Unknown entities return 404; conflicts return 409; explicitly expired checkouts return 410. A transport/adapter error is **not a decline**: the tap returns 202 with AUTHORIZING and a safe exception type in `payment.last_error`. Reconciliation keeps the original checkout key.

## Checkout creation
`POST /api/checkouts`, optional UUID `Idempotency-Key` header:
```json
{"merchant_id":"merchant-demo","terminal_id":"galaxy-01","amount_krw":4500,"expires_in_seconds":300}
```
`expires_in_seconds` defaults to 300 (1–3600). Server generates `checkout_id`. HTTP 201 for a new checkout, 200 for the same creation key and identical input; reuse with changed input returns 409. Only one WAITING_FOR_CARD or AUTHORIZING checkout is permitted per terminal, enforced by a partial unique index. Merchant must own the registered, enabled terminal. There is no browser-supplied customer or authorization result.

Checkout response (also returned by GET/cancel):
```json
{
  "checkout_id":"<uuid>", "merchant_id":"merchant-demo", "terminal_id":"galaxy-01",
  "amount_krw":4500, "currency":"KRW", "status":"WAITING_FOR_CARD",
  "created_at":"<UTC>", "expires_at":"<UTC>", "updated_at":"<UTC>",
  "payment_id":null, "payment":null
}
```
Once a payment exists, `payment` contains the financial projection below. Amount and merchant/terminal association are immutable through the API.

## Endpoints
| Method/path | Result |
| --- | --- |
| GET /api/health | 200 service health |
| GET /api/capabilities | 200 demo flags and supported assets |
| POST /api/checkouts | 201 new Checkout; 200 creation-key replay |
| GET /api/checkouts/{checkout_id} | 200 Checkout with latest payment projection |
| GET /api/terminals/{terminal_id}/active-checkout | 200 `{checkout:Checkout|null,latest_event_id}`; unknown/disabled terminal 404 |
| POST /api/checkouts/{checkout_id}/cancel | 200 Checkout; WAITING_FOR_CARD only, cancellation replay is harmless; otherwise 409 |
| POST /api/nfc/tap | 200 definitive authorization/decline; 202 uncertain/in-flight authorization |
| GET /api/checkouts/{checkout_id}/events | SSE replay, latest snapshot and live events |
| GET /api/ops/payments | `{items,total,limit,offset,latest_event_id}`; limit 1–100, default 50; offset ≥0; optional settlement_status filter |
| GET /api/ops/payments/{payment_id} | 200 Payment; unknown UUID 404 |
| GET /api/ops/events | SSE for all checkouts/payments; snapshot contains latest 100 payments plus total |
| POST /api/demo/reset | Protected mock-only reset; see below |

There are no public financial mutation or recovery endpoints. Operations APIs are read-only. Health reports process availability, not the health of an external financial service.

## NFC tap
```json
{
  "terminal_id":"galaxy-01",
  "card_alias":"demo-alice",
  "client_event_id":"<uuid>",
  "checkout_id":"<uuid>"
}
```
`checkout_id` is optional: omission resolves the terminal's current active checkout. **Send it whenever possible** to bind delayed taps to the intended purchase. A delayed event with a new client_event_id and no checkout_id cannot be distinguished from a tap for the current checkout. The backend validates the terminal and card, finds/checks the checkout, and resolves its amount and demo customer from SQLite.

Tap response: `{checkout:Checkout,payment:Payment,duplicate:boolean,latest_event_id:integer}`. A definitive decline still returns HTTP 200; inspect the server state. Reusing client_event_id with the same input returns the same payment, including after checkout completion. Changed input with the same event ID returns 409. Different event IDs bound to the same checkout and customer also return the same payment. Another customer cannot claim a checkout already associated with a payment. Invalid/unknown taps create no payment; only accepted tap events are persisted for deduplication.

Unknown/disabled card: 404 UNKNOWN_CARD. Unknown/disabled terminal: 404 UNKNOWN_TERMINAL. No active checkout: 409 NO_ACTIVE_CHECKOUT. Explicit checkout from another terminal: 404 CHECKOUT_NOT_FOUND. Expired explicit checkout: 410 CHECKOUT_EXPIRED.

## Payment projection
Fields: payment_id, checkout_id, idempotency_key (=checkout_id), merchant_id, terminal_id, customer_id, amount_krw, currency, authorization_status, asset_reservation_status, prefunding_status, merchant_payout_status (optional), liquidation_status, reimbursement_status, settlement_status, source, engine_payment_id, engine_version, reason_code, last_error, assets, timestamps, settlement_reference, created_at, updated_at, authorized_at, settled_at.

States:
- Authorization: PENDING, AUTHORIZED, DECLINED.
- Asset reservation: NOT_STARTED, PENDING, RESERVED, FAILED, RELEASED.
- Company prefunding: NOT_STARTED, PENDING, RESERVED, FAILED.
- Merchant payout (optional): NOT_STARTED, PENDING, COMPLETED, FAILED; null means unknown/unreported. Capacity reservation does not imply payout completion.
- Liquidation: NOT_STARTED, PENDING, PROCESSING, COMPLETED, FAILED.
- Reimbursement: NOT_STARTED, PENDING, COMPLETED, FAILED.
- Overall settlement: NOT_STARTED, PENDING, COMPLETED, RECOVERY_REQUIRED.

merchant_payout_status is an additive nullable field on EnginePayment and PaymentRead, persisted in payments.engine_detail JSON. Existing SQLite schemas, checkout models, request bodies and endpoint paths are unchanged; no migration or request change is required. The existing asset_reservation_status field (the reservation projection) and prefunding/liquidation/reimbursement enums retain their names and meanings. Existing event payloads without the optional field remain valid.

New approval requires confirmed sufficient wallet-held assets, reserved company KRW capacity and an explicit engine-reported merchant_payout_status=COMPLETED. AUTHORIZED with a reported NOT_STARTED/PENDING/FAILED payout is inconsistent and rejected. Missing/null payout data is accepted for wire/legacy compatibility but cannot authorize a new payment: it remains AUTHORIZING/202 pending an authoritative engine update. Already-authorized legacy records remain readable and can continue settlement with payout unknown; no retroactive payout or inferred success is recorded. A known COMPLETED payout cannot regress or become unknown.

All financial fields are reported adapter projections. No balances, financial ledger or on-chain execution exists here. A failed liquidation or reimbursement after authorization retains AUTHORIZED and RESERVED assets and sets RECOVERY_REQUIRED. Company prefunding remains RESERVED because this demo contract does not define a prefunding release state. `settlement_reference` and engine timestamps identify **simulated** receipts in mock mode; unknown information is null or absent.

## SSE replay and reconnect
Both event endpoints accept `Last-Event-ID: <nonnegative integer>` or `?after=<cursor>`; the header takes precedence. Without a cursor, send a current snapshot, then follow new events. With a cursor, replay persisted events after that cursor up to a consistent snapshot boundary, emit a fresh snapshot, then follow live events. SSE IDs are durable monotonically increasing SQLite AUTOINCREMENT values, preserved across demo resets. Events are at-least-once; clients deduplicate IDs. A snapshot may reuse the last replayed ID and must still be applied as a snapshot.

`follow=false` returns a **finite SSE response**, useful in Swagger, curl and automated checks. Default follow=true stays open, checks for client disconnect and sends `: keepalive` every 15 seconds. Polling the SQLite event table keeps streams consistent across multiple app instances; no in-memory subscription is required. Cache-Control is no-store and X-Accel-Buffering is no.

```text
id: 12
event: payment.authorization
data: {"schema_version":1,"event_id":12,"event_type":"payment.authorization","checkout_id":"...","payment_id":"...","source":"simulated","occurred_at":"...","payload":{"checkout":{...}}}

```
Names: checkout.created, checkout.status_changed, payment.authorization, reservation.status_changed, prefunding.status_changed, merchant_payout.status_changed, liquidation.status_changed, reimbursement.status_changed, settlement.status_changed, payment.updated, payment.reconciliation_pending, demo.reset. Financial events contain the full correlated checkout/payment snapshot at event time. `snapshot` payload is `{checkout,latest_event_id}` for a checkout or the payments list envelope for operations. A future/unavailable cursor emits `resync` and a snapshot. No event retention pruning occurs except explicit demo reset. Reset emits a global demo.reset event so clients can clear cached history.

## Protected reset
Set `VAULTPAY_DEMO_RESET_TOKEN` before starting, then send it as `X-Demo-Reset-Token`. Missing configuration returns 503; missing/wrong header returns 403. Never put the token in URLs. Resets clear simulated checkout/payment/tap/mock-engine/event rows, preserve terminal/customer/card registrations and append demo.reset at a higher cursor. Pending AUTHORIZING checkouts block reset with 409. External adapter mode also blocks reset. Authorization has no browser recovery override; a stuck real integration must be reconciled by its owner.

## Shared adapter contract
`PaymentAdapter.source`: simulated or external. Async methods:
- `authorize(AuthorizationRequest) -> EnginePayment`
- `get_payment(checkout_id: UUID) -> EnginePayment`

AuthorizationRequest includes payment_id, checkout_id, merchant_id, terminal_id, customer_id, server-resolved amount_krw, currency. `idempotency_key` property returns checkout_id. EnginePayment includes correlation IDs, amount/currency, all financial states, source, monotonically increasing version, eligible assets, reason_code, timestamps and settlement_reference. Validate both reservations and explicit completed merchant payout before new approval; reject correlation, amount, provenance, invalid assets, authorization reversals and completed-stage regressions. Duplicate or older versions create no duplicate financial events.

`EnginePaymentNotFound` means the adapter can **definitively guarantee absence**; only then can a worker resubmit with the same checkout key. All other exceptions remain uncertain and are polled again. Worker leases serialize reconciliation across instances. A crash can delay recovery until the persisted lease expires (normally 10 seconds, longer if the engine timeout exceeds 8 seconds).

The methods remain a POS-to-Payment-Engine contract. Wallet holds, available balance/custody, wallet-controlled DEX execution, treasury delivery and financial compensation are internal external-engine dependencies. POS never calls those systems. The engine must deduplicate holds, payouts and liquidation instructions downstream using the same checkout idempotency scope; POS-level deduplication alone is not that external guarantee.

MockPaymentAdapter persists deterministic simulated engine responses in its own SQLite table and progresses through settlement based on elapsed time. This makes demo recovery survive process restart. ExternalPaymentAdapter is an explicit NotImplemented placeholder, makes no network calls and cannot be selected through an environment toggle. A future HTTPX integration must agree authenticated paths, checkout-key idempotency, lookup consistency, versions, failure/recovery semantics and timeouts with the engine team before being enabled.

## Mock orchestration clarification
The normal mock confirms a scripted wallet hold, reserves scripted company KRW capacity, records a completed simulated merchant payout and then returns AUTHORIZED. Merchant payout status/timestamp are stable on retries. Background projection timestamps include wallet_liquidation_requested_at, liquidation_started_at, liquidation_completed_at, treasury_received_at, reimbursed_at and released_at. All are simulated, with no external wallet/DEX/bank calls.

New demo card demo-payout-fail maps to a payout-failure customer. The mock reports asset/prefund RESERVED, merchant payout FAILED and a definitive DECLINED with DEMO_MERCHANT_PAYOUT_FAILED, NOT_STARTED liquidation/reimbursement/settlement and no merchant_paid_at. It does not silently release holds or initiate liquidation; pre-authorization compensation is engine-owned. Existing demo cards retain their prior behavior. Post-authorization liquidation/reimbursement failures still report RECOVERY_REQUIRED with authorization and held assets preserved.
