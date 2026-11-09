"""One short-lived operator session on the dedicated loopback demo listener."""
import hmac
import secrets
import time
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

HOST = "127.0.0.1"
PORT = 8000
ORIGIN = f"http://{HOST}:{PORT}"
COOKIE = "vaultpay_demo_ops"
SESSION_SECONDS = 900

LOGIN_FORM = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>VaultPay Local Operations</title>
<main><h1>VaultPay Local Operations</h1>
<form method="post" action="/ops/login">
<label>Demo access code <input name="code" type="password" required></label>
<button>Sign in</button>
</form></main></html>"""


class LocalOpsSession:
    def __init__(self, access_code: str):
        self._access_code = access_code.encode()
        self._session: tuple[str, float] | None = None

    @staticmethod
    def is_local(request: Request) -> bool:
        return (
            request.client is not None and request.client.host == HOST
            and request.scope.get("server") == (HOST, PORT)
            and request.url.scheme == "http" and request.headers.get("host") == f"{HOST}:{PORT}"
            and not any(name in ("forwarded", "via", "x-real-ip") or name.startswith(("x-forwarded-", "cf-"))
                        for name in request.headers)
        )

    def authorized(self, request: Request) -> bool:
        session = self._session
        if session is None or not self.is_local(request):
            return False
        token, expires = session
        return time.monotonic() < expires and hmac.compare_digest(
            request.cookies.get(COOKIE, "").encode(), token.encode(),
        )

    @staticmethod
    def form(status_code: int = 200):
        return HTMLResponse(LOGIN_FORM, status_code=status_code, headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        })

    async def login(self, request: Request):
        if not self.is_local(request) or request.headers.get("origin") != ORIGIN:
            return self.form(403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            return self.form(415)

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1024:
                return self.form(413)
            body.extend(chunk)
        try:
            code = parse_qs(body.decode(), max_num_fields=1).get("code", [""])[0]
        except ValueError:
            return self.form(403)
        if not hmac.compare_digest(code.encode(), self._access_code):
            return self.form(403)

        token = secrets.token_urlsafe(32)
        self._session = (token, time.monotonic() + SESSION_SECONDS)
        response = RedirectResponse("/ops", status_code=303, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict")
        return response
