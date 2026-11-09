import hmac, hashlib, json, urllib.request, urllib.error

BASE = "https://oregon-visited-wichita-install.trycloudflare.com"
SECRET = b"여기에_HMAC_키"

body = json.dumps({
    "checkout_id": "co_test_001",
    "amount_krw": 4500,
    "merchant_id": "cafe",
    "card_token": "91ea67e855c23f69148746d73bf9c5d290227c0c4ab762f3fcc84713512d892b",
}, separators=(",", ":")).encode()
sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()

req = urllib.request.Request(f"{BASE}/v1/payments/authorize", data=body, method="POST",
    headers={"Content-Type": "application/json", "X-SB-Signature": sig})
try:
    print(urllib.request.urlopen(req).read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())