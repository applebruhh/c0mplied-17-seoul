# VaultPay API contract · v1 proposal

All public paths are under `/api/v1`. JSON uses snake_case and UTC ISO-8601 timestamps. KRW amounts are strict positive integers (1–10,000,000 for demo validation), never floats; crypto quantities are decimal strings. Reject unknown fields in command bodies. Current implemented endpoints: health and capabilities only. All payment endpoints below are contracts for the next milestone, not functioning API routes.

## Common types
Checkout: `{id, terminal_id, amount_krw, currency:"KRW", status, created_at, expires_at, payment_id:null|uuid}`.
Payment: `{id, checkout_id, customer_id, amount_krw, currency:"KRW", authorization_status, settlement_status, asset_reservation_status, krw_reservation_status, source:"simulated"|"external", engine_payment_id:null|string, reason_code:null|string, created_at, authorized_at:null|timestamp, settled_at:null|timestamp, updated_at}`.
Asset reservation detail: `{asset:"ETH"|"BTC"|"USDC", quantity:string, buffer_bps:integer, engine_hold_id:string}`. Financial amounts and hold IDs originate from the engine only. Optional settlement detail includes swap reference, settlement USDC quantity, treasury receipt reference, reimbursement status and release details. Unknown values are null, never fabricated zeroes.

Errors (planned command endpoints): `{error:{code,message,request_id}}`. HTTP 422 validation; 404 unknown entity; 409 active checkout conflict, idempotency conflict or invalid state; 410 expired checkout; 503 engine unavailable before submission. If submission may have reached the engine, retain PROCESSING and return 202 instead of claiming a decline. Scaffold FastAPI validation currently uses its standard `detail` format until the payment API error handler is implemented.

## Endpoints
| Method/path | Input | Result |
| --- | --- | --- |
| GET /health | — | 200 `{status:"ok", service:"vaultpay-pos", milestone:"scaffold"}` |
| GET /capabilities | — | 200 implemented features and supported assets |
| POST /checkouts | Idempotency-Key header; `{terminal_id, amount_krw}` | 201 Checkout; exact replay 200 |
| GET /checkouts/{id} | — | 200 Checkout |
| GET /terminals/{id}/active-checkout | — | 200 `{checkout:Checkout|null}` |
| POST /checkouts/{id}/cancel | `{}` | 200 Checkout, WAITING only |
| POST /taps | Idempotency-Key header; `{terminal_id, checkout_id, card_id}` | 202 `{checkout,payment}` after durable claim; replay returns same payment |
| GET /payments/{id} | — | 200 Payment plus engine-provided reservation and settlement detail |
| GET /payments | `limit=50` (1–100), `before` cursor, optional settlement filter | 200 `{items:Payment[], next_cursor:string|null}` ordered by created_at/id descending |
| GET /events | Last-Event-ID header or `after` cursor; optional terminal_id | text/event-stream |

No browser endpoint can set financial status or instruct a release. Ops uses read endpoints. No recovery mutation endpoint is exposed; engine owns recovery. Amount is read from the persisted checkout on every authorize call. History retains approved transactions even when later settlement requires recovery.

## Idempotency
Require a UUID request key for checkout creation and each logical tap. Persist canonical payload hashes; reuse with different payload returns 409. Identical retries replay the same resource. A payment UUID is the engine key for the entire payment lifecycle; never create a new key because of a timeout. Different tap keys against the same checkout converge on its unique payment, with no second engine authorization. Inactive, disabled or unknown cards fail validation before the engine. Prevent a delayed tap from authorizing a newer checkout by requiring its explicit checkout ID.

## SSE
```text
id: 42
event: payment.updated
data: {"schema_version":1,"event_id":42,"source":"simulated","occurred_at":"2026-11-09T01:00:00Z","checkout_id":"...","payment_id":"...","payload":{}}

```
Event names: checkout.created, checkout.updated, payment.updated. Event envelope includes schema version, durable event ID, source, timestamp and correlated IDs. Heartbeat `: keepalive` every 15 seconds. At-least-once delivery; deduplicate event IDs. Native EventSource sends Last-Event-ID on reconnect. A newly mounted client fetches its snapshot then replays from a server snapshot cursor (to be included in implementation); retain all demo events. Return an explicit resync event if a requested cursor is unavailable. Keep events free of secrets and unneeded customer information.

## Proposed external engine contract
This is a proposed integration boundary, not an assertion about another team's API. `ExternalPaymentAdapter` implements the same Python protocol as the mock, but must not be enabled until paths, authentication and semantics are accepted by the engine team.

- POST `{PAYMENT_ENGINE_URL}/authorizations`, `Authorization: Bearer …`, `Idempotency-Key: <payment UUID>`; body `{payment_id, checkout_id, customer_id, amount_krw, currency:"KRW"}`. Response 200 `EnginePayment` as defined by Pydantic.
- GET `{PAYMENT_ENGINE_URL}/authorizations/by-key/{payment UUID}` returns the same model, including when the authorize response was lost. 404 means unresolved until the engine's consistency guarantee permits retry with the same key.
- EnginePayment includes the original payment key and amount/currency, engine ID, authorization, settlement, separate hold/prefund states, eligible assets, source, reason and server timestamps where known. The current scaffold model contains the minimal projection; add detailed receipts once agreed.
- HTTP timeout/invalid response is uncertain authorization. Orchestration reconciles; it never converts a transport exception into DECLINED. Adapter rejects mismatched IDs/amounts and unsupported assets.
- Future callbacks must authenticate raw payload signatures, deduplicate upstream event IDs and enforce monotonic engine versions. Do not expose an unauthenticated financial webhook.
