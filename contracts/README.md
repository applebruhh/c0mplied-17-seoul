# Shared contract freeze: vaultpay.pos.v1

Canonical Python schemas: backend/app/models.py (POS/internal adapter) and backend/app/contracts.py (SB wire, domain errors and parsed SSE). Generated TypeScript: frontend/src/shared/contracts.ts. Import types from that file in Phase 4–6; do not copy interfaces into individual screens.

Artifacts:
- pos.openapi.json: existing public POS HTTP routes and request/response validation.
- shared.schema.json: named JSON Schema definitions under #/$defs/<ModelName>, including internal authorization results, POS responses, errors and parsed SSE frame types.
- payment-engine.schema.json: external authorize request/response schemas.
- freeze.json: contract version, artifact SHA-256 hashes, endpoint/header identifiers and team UID test vector.

The JSON files define runtime bounds, formats, nullability and required fields. TypeScript types express shapes; a TypeScript number does not itself validate integer KRW bounds. SSEFrame describes an already-parsed SSE frame, not a new JSON HTTP endpoint. EventRead.payload remains open for compatibility; CheckoutEventPayload describes checkout financial events, while demo.reset has a reset payload. Unknown future SSE event names should be ignored safely by clients.

Verify from the repository root:
```sh
.venv/bin/python scripts/freeze_contracts.py --check
npm --prefix frontend run typecheck
```
After a coordinated contract change, regenerate once:
```sh
.venv/bin/python scripts/freeze_contracts.py
```
Do not hand-edit generated artifacts. The backend test suite fails if artifacts drift from live Pydantic/OpenAPI definitions. The freeze is a checked-in compatibility baseline, not a claim that the external engine was contacted. Changes to requests, enum meanings, routes, nullability or event envelopes require shared-owner review and a documented version decision before parallel lanes regenerate together. No lane may independently regenerate a breaking contract to make its tests pass.

See docs/api-contract.md for wire signing and known integration gaps, and docs/parallel-development.md for Phase 4–6 ownership.
