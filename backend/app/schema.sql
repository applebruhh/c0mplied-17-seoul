-- Initial design. Payment services and migrations are a later milestone.
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_cards (
    card_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS terminals (
    id TEXT PRIMARY KEY, display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkouts (
    id TEXT PRIMARY KEY, terminal_id TEXT NOT NULL REFERENCES terminals(id),
    creation_key TEXT NOT NULL UNIQUE, creation_fingerprint TEXT NOT NULL,
    amount_krw INTEGER NOT NULL CHECK(typeof(amount_krw) = 'integer' AND amount_krw BETWEEN 1 AND 10000000),
    currency TEXT NOT NULL DEFAULT 'KRW' CHECK(currency = 'KRW'),
    status TEXT NOT NULL CHECK(status IN ('WAITING','PROCESSING','APPROVED','DECLINED','CANCELLED','EXPIRED')),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_checkout_per_terminal
ON checkouts(terminal_id) WHERE status IN ('WAITING','PROCESSING');
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, checkout_id TEXT NOT NULL UNIQUE REFERENCES checkouts(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    engine_key TEXT NOT NULL UNIQUE, engine_payment_id TEXT UNIQUE,
    authorization_status TEXT NOT NULL CHECK(authorization_status IN ('PENDING','UNKNOWN','AUTHORIZED','DECLINED')),
    settlement_status TEXT NOT NULL CHECK(settlement_status IN ('NOT_STARTED','PENDING','SETTLED','RECOVERY_REQUIRED')),
    asset_reservation_status TEXT NOT NULL CHECK(asset_reservation_status IN ('NONE','RESERVED','PARTIALLY_RELEASED','RELEASED','UNKNOWN')),
    krw_reservation_status TEXT NOT NULL CHECK(krw_reservation_status IN ('NONE','RESERVED','PARTIALLY_RELEASED','RELEASED','UNKNOWN')),
    source TEXT NOT NULL CHECK(source IN ('simulated','external')),
    reason_code TEXT, engine_version INTEGER NOT NULL DEFAULT 0,
    engine_detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, authorized_at TEXT, settled_at TEXT, updated_at TEXT NOT NULL,
    CHECK(authorization_status = 'AUTHORIZED' OR settlement_status = 'NOT_STARTED')
);
CREATE TABLE IF NOT EXISTS tap_requests (
    request_key TEXT PRIMARY KEY, payload_fingerprint TEXT NOT NULL,
    checkout_id TEXT NOT NULL REFERENCES checkouts(id),
    payment_id TEXT NOT NULL REFERENCES payments(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_events (
    engine_event_id TEXT PRIMARY KEY, payment_id TEXT NOT NULL REFERENCES payments(id),
    engine_version INTEGER NOT NULL, payload_json TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    checkout_id TEXT REFERENCES checkouts(id), payment_id TEXT REFERENCES payments(id),
    source TEXT NOT NULL CHECK(source IN ('simulated','external')),
    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_checkout_cursor ON events(checkout_id, id);
CREATE INDEX IF NOT EXISTS payments_reconciliation ON payments(authorization_status, settlement_status);
