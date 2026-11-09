# Stockbucks Payment Engine · Phase 7A

## Backend configuration

| Variable | Behavior |
| --- | --- |
| `PAYMENT_ENGINE_MODE` | `mock` by default. `external` enables the HTTP adapter and internal Ops polling routes. |
| `PAYMENT_ENGINE_BASE_URL` | Required in external mode. HTTP(S) origin with no path, query, fragment or embedded credentials. Use an HTTPS engine origin for rehearsal. |
| `SB_POS_HMAC_SECRET` | Required in external mode. Server-only UTF-8 HMAC key for POS authorization. |
| `VAULTPAY_OPS_TOKEN` | Separate server-side bearer credential for internal Ops. Without it, Ops routes in external mode return 503. Setting it in mock mode also protects local Ops. |
| `VAULTPAY_DATABASE_PATH` | Use a separate SQLite file for the external environment. Keep it across restarts so checkout IDs and request snapshots survive. |
| `VAULTPAY_ENGINE_TIMEOUT` | Request deadline in seconds, default 3. |

Export variables into the backend process. Start the backend from `backend/` with `uvicorn app.main:app`. The example environment file leaves all credentials blank. Send the Ops bearer only from an authorized internal client over a trusted connection.

## Confirmed contract and gaps

Source is the team-provided Stockbucks Payment Engine v0.1.0 OpenAPI 3.1 export (SHA-256 `341b5af88d06c1411adb909f83f208d4ea57a72e0fa030503e22e6138c0c7ba3`). The temporary tunnel's OpenAPI request timed out. Transport and POS projection were exercised with local HTTPX stubs.

- `POST /v1/payments/authorize` requires `checkout_id`, `amount_krw` and `merchant_id` upstream. The engine accepts positive numeric amounts. VaultPay sends its persisted whole-KRW checkout amount, persisted merchant, and enrolled `card_token`. The engine's optional `wallet_id` is omitted. The checkout ID is the upstream idempotency key.
- The supplied POST contract requires lowercase HMAC-SHA256 of the exact UTF-8 request bytes in `X-SB-Signature`. The OpenAPI export has no security declaration. The signature format comes from the team contract. The backend sends the same stored bytes through HTTPX `content=`.
- Authorization and POS status have empty 200 schemas. The five-field authorization envelope and four business decline reasons come from the team contract. GET reuses that envelope as a provisional parser and signs an empty body using the existing adapter convention. Actual GET signing, response fields, missing-checkout behavior and non-200 semantics need verification. The OpenAPI describes the status route as approval-only.
- Ops list, detail, alerts and decisions have empty 200 schemas. No financial fields or response wrappers are inferred. Authenticated internal clients receive opaque JSON. The adapter sends no upstream Ops credentials pending confirmation of the engine's Ops access policy.
- `POST /v1/cards` takes `token` and `wallet_id`. Authorization uses `card_token`. `POST /v1/merchants` requires `name` and accepts optional `id`, `bank_code`, `account_num` and `account_holder`. POS uses its existing merchant configuration. The backend issues neither registration request.

## NFC and card enrollment

The frozen NFC field is `card_alias`. It accepts the Galaxy UID or the iPhone Shortcut alias. Raw UID lookups strip colons, hyphens and whitespace, uppercase complete ASCII hex pairs in the original order, and hash the resulting ASCII text with SHA-256. Both paths resolve through a local enrollment. Ordinary mock aliases and the mock seed UID require explicit enrollment for external use.

In Phase 7B, have the engine owner verify the approved test wallet and whether the token is already registered. If registration is needed, the owner can submit `POST /v1/cards` once after confirming the engine's duplicate policy. Use this backend-only helper to validate the request body without sending it.

```python
from app.contracts import ExternalCardRegistration
from app.signing import card_token_from_uid

payload = ExternalCardRegistration(token=card_token_from_uid(uid), wallet_id=test_wallet_id)
body = payload.model_dump_json()
```

After verifying the engine-side registration, bind the same UID and a dedicated Shortcut alias to a known local customer using the external database. The helper reuses identical bindings and rejects conflicts.

```python
from app.cards import enroll_external_card
from app.config import Settings
from app.db import Database

db = Database(Settings.from_env().database_path)
try:
    db.initialize()
    with db.transaction() as session:
        enroll_external_card(session, card_alias=shortcut_alias, uid=uid, customer_id=local_customer_id)
finally:
    db.close()
```

## Recovery and Operations

An ambiguous POST outcome is exposed as `UNKNOWN` with the original checkout still `AUTHORIZING`. The worker looks up the same checkout ID. It can retry with the stored command if the local dispatch claim was never made or if HTTPX reported that it never acquired a connection. The retry uses the identical serialized bytes when a wire snapshot already exists. An ambiguous delivery, malformed reply or upstream 404 stays unresolved. A crash after the dispatch claim may require engine-side confirmation in Phase 7B. A business decline requires a valid application-level `DECLINED` response.

| Internal route | Source |
| --- | --- |
| `/api/ops/payments`, `/api/ops/payments/{payment_id}` | Existing local list and detail projections, with local UUIDs and pagination |
| `/api/ops/events` | Existing durable local SSE stream |
| `/api/ops/engine/payments` | Upstream list. Optional `limit` defaults to 50 |
| `/api/ops/engine/payments/{pid}` | Upstream detail by engine payment ID |
| `/api/ops/alerts` | Upstream alerts. Optional `audience` and `limit` (default 100) |
| `/api/ops/decisions` | Upstream decisions. Optional `kind`, `ref_id` and `limit` (default 20) |

The new routes accept GET only, bound list sizes and concurrent upstream polling, and return responses with `Cache-Control: no-store`. They are registered only in external mode. External mode also protects the existing local Ops routes. The checkout routes retain their existing contract. The React dashboard integration with internal authentication and real Ops field mapping belongs to follow-up work.

## Phase 7B setup

1. Obtain the current HTTPS origin, a test POS HMAC key and the upstream Ops access policy from the engine owner. Configure a separate local Ops token.
2. Confirm the existing `cafe` test merchant, local terminal mapping and the designated test wallet. Have the engine owner verify the card registration, then enroll the matching UID locally.
3. Collect sanitized response examples for authorization, POS status and each Ops endpoint. Verify GET signing, missing-checkout semantics and when a same-key replay is permitted before enabling any retry after uncertain delivery. Verify downstream idempotency for holds, payouts and liquidation.
4. Confirm the private Ops access path for operators. Map verified Ops data to frontend fields in a coordinated integration change.
