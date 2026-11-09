import httpx
import pytest

from app.config import Settings
from app.local_ops import COOKIE, ORIGIN
from app.main import create_app

CODE = "local-operator-fixture-00000000"
BEARER = {"Authorization": "Bearer normal-ops-fixture"}


def build_app(path, *, flag, dedicated):
    return create_app(
        settings=Settings(
            database_path=path, payment_engine_mode="external", payment_engine_base_url="https://engine.test",
            sb_pos_hmac_secret="engine-fixture", ops_token="normal-ops-fixture",
            demo_local_ops=flag, demo_local_ops_access_code=CODE,
        ),
        engine_transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
        local_demo_listener=dedicated,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(("flag", "dedicated"), [(False, True), (True, False)])
async def test_local_ops_requires_flag_and_dedicated_listener(tmp_path, flag, dedicated):
    app = build_app(tmp_path / "ops.sqlite3", flag=flag, dedicated=dedicated)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.ASGITransport(app=app)) as client:
            assert (await client.get("/ops")).status_code == 404
            assert (await client.post("/ops/login", data={"code": CODE})).status_code == 404
            assert (await client.get("/api/ops/payments")).status_code == 403
            assert (await client.get("/api/ops/payments", headers=BEARER)).status_code == 200


@pytest.mark.anyio
async def test_local_ops_session_is_direct_short_lived_and_projection_only(tmp_path, monkeypatch):
    app = build_app(tmp_path / "ops.sqlite3", flag=True, dedicated=True)
    now = 100
    monkeypatch.setattr("app.local_ops.time", type("Clock", (), {"monotonic": staticmethod(lambda: now)}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.ASGITransport(app=app)) as client:
            assert 'type="password"' in (await client.get("/ops")).text
            assert (await client.get("/api/ops/payments")).status_code == 403
            assert (await client.post("/ops/login", data={"code": CODE})).status_code == 403
            assert (await client.post("/ops/login", data={"code": "wrong"}, headers={"Origin": ORIGIN})).status_code == 403
            logged_in = await client.post("/ops/login", data={"code": CODE}, headers={"Origin": ORIGIN})
            assert logged_in.status_code == 303
            assert logged_in.headers["location"] == "/ops"
            cookie = logged_in.headers["set-cookie"].lower()
            assert all(flag in cookie for flag in ("httponly", "samesite=strict", "max-age=900"))
            assert CODE not in logged_in.headers["set-cookie"]
            assert (await client.get("/api/ops/payments")).status_code == 200
            assert (await client.get("/api/ops/payments", headers={"X-Forwarded-For": "127.0.0.1"})).status_code == 403
            assert (await client.get("/api/ops/payments", headers={"Host": "tunnel.example"})).status_code == 403
            assert (await client.get("/api/ops/engine/payments")).status_code == 403
            assert (await client.get("/api/ops/engine/payments", headers=BEARER)).status_code == 200
            async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.ASGITransport(
                app=app, client=("203.0.113.12", 5000),
            )) as remote:
                spoofed = {"X-Forwarded-For": "127.0.0.1", "Cookie": f"{COOKIE}={client.cookies[COOKIE]}"}
                assert (await remote.get("/api/ops/payments", headers=spoofed)).status_code == 403
            now += 900
            assert (await client.get("/api/ops/payments")).status_code == 403
