# VaultPay Galaxy NFC terminal

## Run on a Galaxy

Install Python 3.11+, Node 20.11+, npm, and Android Platform Tools (`adb`). From the repository root on the laptop:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e './backend[dev]'
npm --prefix frontend ci
npm --prefix frontend run build
cd backend
../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

FastAPI serves the built terminal and the payment API on the same port. Rebuild the frontend after editing it. For a hot-reloading desktop session, leave FastAPI running and run `npm --prefix frontend run dev` from the repository root. Vite serves `/terminal` on port 5173 and proxies API requests to port 8000.

Enable NFC and USB debugging on the Galaxy, connect it by USB, and accept the debugging prompt. On the laptop:

```sh
adb devices
adb reverse tcp:8000 tcp:8000
```

Open [http://localhost:8000/terminal](http://localhost:8000/terminal) directly in Galaxy Chrome. Web NFC needs a secure, top-level page and a user gesture. The forwarded `localhost` origin provides that context. Use a compatible NDEF tag or card whose serial number Chrome can read. See [Chrome's Web NFC guide](https://developer.chrome.com/docs/capabilities/nfc).

The terminal defaults to `cafe-01`. For another registered terminal, append `?terminal_id=galaxy-01` and use the matching merchant and terminal when creating a checkout. To create the default terminal's ₩4,500 checkout from the laptop:

```sh
curl -fsS http://localhost:8000/api/checkouts \
  -H 'Content-Type: application/json' \
  -d '{"merchant_id":"cafe","terminal_id":"cafe-01","amount_krw":4500}'
```

The terminal discovers the checkout automatically. **Start NFC Scan** prompts for NFC access as needed. The **Test only: Run demo tap** button uses the registered `demo-alice` identity when the backend reports demo mode. Both actions submit the same tap request shape and use the checkout amount from the backend.

If a tap response is lost, the terminal displays `RESULT_UNKNOWN` and checks that checkout by ID. If it is still waiting for a card, **Retry same tap** resends the original request with the same event ID. Keep the tab open while the result is unresolved so it can retain the event ID and continue checking.

When finished, remove the forwarding with `adb reverse --remove tcp:8000`.

## Register a card from the customer app

When no checkout is waiting, the terminal polls `GET /api/terminals/{terminal_id}/pending-enrollment`. A customer who chooses **카드 등록 → 매장 단말기** in `/app` creates a two-minute enrollment request. The terminal switches to card-registration mode; **등록용 NFC 스캔 시작** reads the next tag and posts its UID to `/api/terminals/{terminal_id}/enrollments/{id}/complete`. In mock mode the UID is bound (or re-bound) to that customer. In external mode the UID is bound locally with the existing strict enrollment rules, and the customer app shows the SHA-256 card token for engine-side `POST /v1/cards` registration. A tag scanned while a checkout is waiting is always treated as a payment.

## Register a physical demo tag

The mock database seeds `04A1B2C3D4E5F6`. If your tag has a different UID, scan it once to see the identifier and the unregistered-card message. Register the displayed UID to the existing success demo customer using the following command from `backend`. Use the same `VAULTPAY_DATABASE_PATH` as the running server if you changed it.

```sh
NFC_UID='<paste the displayed UID>' ../.venv/bin/python - <<'PY'
import os

from app.config import Settings
from app.db import Database
from app.signing import normalize_uid
from app.tables import DemoCard

uid = normalize_uid(os.environ["NFC_UID"])
database = Database(Settings.from_env().database_path)
with database.transaction() as session:
    if session.get(DemoCard, uid) is not None:
        raise SystemExit(f"{uid} already has a registration")
    session.add(DemoCard(card_alias=uid, customer_id="customer-success", enabled=True))
print(f"Registered {uid} for the success demo")
PY
```

Start a fresh scan for the same waiting checkout, or create a new checkout if the previous one expired. The terminal sends the uppercase, separator-free UID as `card_alias`. The backend looks up the registered identity and computes the SHA-256 card token. The terminal reads the serial number from Chrome's NFC event.

## Manual hardware checklist

Galaxy hardware verification is pending.

- Open the forwarded URL on Galaxy Chrome, create a checkout, and verify that the phone shows its KRW amount.
- Tap **Start NFC Scan** and deny access. Verify the error, then allow NFC in Chrome site settings and start again.
- Scan a registered NDEF tag. Verify that its UID is displayed, then verify the authorization result against `GET /api/checkouts/{checkout_id}`.
- Hold the tag against the phone and tap it again without starting a new scan. Verify one tap request and one payment for the checkout.
- Try an unregistered tag and verify the backend error. If you have a tag that exposes no UID, verify that the terminal shows a UID error and sends no tap.
- Try with NFC turned off. Then enable it, start a scan, and switch away from Chrome. Return and verify that you can start a new scan.
- Create a fresh checkout and run the mock-only test button. Verify that the backend authorizes or declines it using its normal checkout flow.
