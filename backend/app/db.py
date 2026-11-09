from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.tables import Base, Customer, DemoCard, Merchant, MockCompanyCapacity, MockWallet, Terminal
from app.adapters.mock_fixtures import COMPANY_CAPACITY_KRW, DEMO_CARDS, holdings
from app.migrations import upgrade_authorization_unknown


def seed_mock_wallets(session):
    day = datetime.now(timezone.utc).date().isoformat()
    if not session.get(MockCompanyCapacity, "demo"):
        session.add(MockCompanyCapacity(name="demo", available_krw=COMPANY_CAPACITY_KRW))
    for _, scenario in DEMO_CARDS:
        wallet_id = f"wallet-{scenario}"
        if not session.get(MockWallet, wallet_id):
            balances = holdings(scenario)
            session.add(MockWallet(
                wallet_id=wallet_id, customer_id=f"customer-{scenario}", holdings=balances,
                lock_checkout_id="fixture-busy-owner" if scenario == "wallet_busy" else None,
                hold_expires_at=None, sell_day=day, daily_basis=balances.copy(), daily_sold={},
            ))


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 10000")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.close()

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        inspector = inspect(self.engine)
        if inspector.has_table("checkouts"):
            columns = {column["name"] for column in inspector.get_columns("checkouts")}
            if "merchant_id" not in columns:
                raise RuntimeError(
                    "Phase 1 schema detected. Preserve that file and select a new "
                    "VAULTPAY_DATABASE_PATH; automatic destructive migration is disabled."
                )
        Base.metadata.create_all(self.engine)
        upgrade_authorization_unknown(self.engine)
        with self.transaction() as session:
            if not session.get(Merchant, "merchant-demo"):
                session.add(Merchant(merchant_id="merchant-demo", display_name="VaultPay Demo Cafe"))
                session.flush()
            for terminal_id in ("galaxy-01", "galaxy-02"):
                if not session.get(Terminal, terminal_id):
                    session.add(Terminal(terminal_id=terminal_id, merchant_id="merchant-demo", enabled=True))
            for alias, scenario in DEMO_CARDS:
                customer_id = f"customer-{scenario}"
                if not session.get(Customer, customer_id):
                    session.add(Customer(customer_id=customer_id, display_name=f"Demo {scenario}"))
                    session.flush()
                if not session.get(DemoCard, alias):
                    session.add(DemoCard(card_alias=alias, customer_id=customer_id, enabled=True))
            session.flush()
            seed_mock_wallets(session)

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[Session]:
        with self.sessions() as session:
            try:
                # Explicit BEGIN also gives reads a consistent snapshot with sqlite3's
                # legacy transaction mode. The write lock is held only for local DB work.
                session.execute(text("BEGIN IMMEDIATE" if write else "BEGIN"))
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def close(self):
        self.engine.dispose()
