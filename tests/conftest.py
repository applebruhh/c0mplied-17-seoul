import os
import sys
import tempfile

os.environ.setdefault("SB_MODE", "sim")
os.environ.setdefault("SB_START_WORKERS", "0")
os.environ.setdefault("SB_DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app import db, runtime  # noqa: E402
from app.config import settings  # noqa: E402
from app.market import prices  # noqa: E402
from app.market.state import MarketState  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    db.rebind("sqlite:///" + str(tmp_path / "t.db"))
    st = MarketState()
    prices.seed_sim(st)
    import app.market.state as ms
    monkeypatch.setattr(ms, "market", st)
    monkeypatch.setattr(runtime, "market", st, raising=False)
    rt = runtime.Runtime()
    rt.market = st
    rt.optimizer.m = st
    rt.chain.state = st
    rt.chain.block_time = 0.0
    monkeypatch.setattr(runtime, "rt", rt)
    monkeypatch.setattr(settings, "hold_ttl_sec", 300)
    yield rt


@pytest.fixture
def setup_user(fresh):
    """사용자 + 지갑 + 카드 + 가맹점. BTC 0.01(140만원), ETH 0.5(245만원), LINK 50(140만원), USDC 300(42만원)."""
    from datetime import datetime, timezone
    from app.models import Card, Merchant, User, Wallet
    rt = fresh
    with db.SessionLocal() as s:
        now = datetime.now(timezone.utc)
        u = User(name="t", risk_profile="neutral", pay_assets=[], delegation_agreed_at=now, lock_notice_agreed_at=now)
        s.add(u)
        s.flush()
        w = Wallet(user_id=u.id, address="0xuser1", provider="sim")
        s.add(w)
        s.flush()
        s.add(Card(token="card1", wallet_id=w.id))
        s.add(Merchant(id="m1", name="카페", bank_code="088", account_num="110123", account_holder="카페"))
        s.commit()
        ids = {"user": u.id, "wallet": w.id}
    for sym, units in {"BTC": 0.01, "ETH": 0.5, "LINK": 50, "USDC": 300}.items():
        rt.chain.fund("0xuser1", sym, units)
    return ids
