-- Generated reference from app.tables.Base metadata; runtime uses SQLAlchemy.
-- Do not hand-edit this file.
PRAGMA foreign_keys = ON;

CREATE TABLE authorization_snapshots (
	checkout_id VARCHAR NOT NULL,
	payment_id VARCHAR NOT NULL,
	request JSON NOT NULL,
	wire_body BLOB,
	submitted BOOLEAN NOT NULL,
	engine_response JSON,
	response_revision INTEGER NOT NULL,
	PRIMARY KEY (checkout_id),
	UNIQUE (payment_id)
);

CREATE TABLE customers (
	customer_id VARCHAR NOT NULL,
	display_name VARCHAR NOT NULL,
	PRIMARY KEY (customer_id)
);

CREATE TABLE merchants (
	merchant_id VARCHAR NOT NULL,
	display_name VARCHAR NOT NULL,
	PRIMARY KEY (merchant_id)
);

CREATE TABLE mock_company_capacity (
	name VARCHAR NOT NULL,
	available_krw INTEGER NOT NULL,
	PRIMARY KEY (name),
	CHECK (available_krw >= 0)
);

CREATE TABLE mock_engine_events (
	event_id VARCHAR NOT NULL,
	payment_id VARCHAR NOT NULL,
	checkout_id VARCHAR NOT NULL,
	customer_id VARCHAR NOT NULL,
	wallet_id VARCHAR NOT NULL,
	event_type VARCHAR NOT NULL,
	timestamp DATETIME NOT NULL,
	state JSON NOT NULL,
	mock_asset_metadata JSON NOT NULL,
	recovery_outcome VARCHAR,
	PRIMARY KEY (event_id)
);

CREATE INDEX ix_mock_engine_events_checkout_id ON mock_engine_events (checkout_id);

CREATE TABLE mock_engine_records (
	checkout_id VARCHAR NOT NULL,
	request_fingerprint VARCHAR NOT NULL,
	request JSON NOT NULL,
	initial_result JSON NOT NULL,
	scenario VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (checkout_id)
);

CREATE TABLE demo_cards (
	card_alias VARCHAR NOT NULL,
	customer_id VARCHAR NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (card_alias),
	FOREIGN KEY(customer_id) REFERENCES customers (customer_id)
);

CREATE TABLE mock_wallets (
	wallet_id VARCHAR NOT NULL,
	customer_id VARCHAR NOT NULL,
	holdings JSON NOT NULL,
	lock_checkout_id VARCHAR,
	hold_expires_at DATETIME,
	sell_day VARCHAR NOT NULL,
	daily_basis JSON NOT NULL,
	daily_sold JSON NOT NULL,
	PRIMARY KEY (wallet_id),
	UNIQUE (customer_id),
	FOREIGN KEY(customer_id) REFERENCES customers (customer_id),
	UNIQUE (lock_checkout_id)
);

CREATE TABLE terminals (
	terminal_id VARCHAR NOT NULL,
	merchant_id VARCHAR NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (terminal_id),
	FOREIGN KEY(merchant_id) REFERENCES merchants (merchant_id)
);

CREATE TABLE external_cards (
	card_alias VARCHAR NOT NULL,
	token VARCHAR NOT NULL,
	PRIMARY KEY (card_alias),
	FOREIGN KEY(card_alias) REFERENCES demo_cards (card_alias),
	UNIQUE (token)
);

CREATE TABLE checkouts (
	checkout_id VARCHAR NOT NULL,
	merchant_id VARCHAR NOT NULL,
	terminal_id VARCHAR NOT NULL,
	amount_krw INTEGER NOT NULL,
	currency VARCHAR NOT NULL,
	status VARCHAR(16) NOT NULL,
	creation_key VARCHAR NOT NULL,
	creation_fingerprint VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (checkout_id),
	CHECK (typeof(amount_krw) = 'integer' AND amount_krw BETWEEN 1 AND 10000000),
	CHECK (currency = 'KRW'),
	FOREIGN KEY(merchant_id) REFERENCES merchants (merchant_id),
	FOREIGN KEY(terminal_id) REFERENCES terminals (terminal_id),
	CONSTRAINT checkoutstatus CHECK (status IN ('WAITING_FOR_CARD', 'AUTHORIZING', 'AUTHORIZED', 'DECLINED', 'CANCELLED', 'EXPIRED')),
	UNIQUE (creation_key)
);

CREATE UNIQUE INDEX one_active_checkout_per_terminal ON checkouts (terminal_id) WHERE status IN ('WAITING_FOR_CARD', 'AUTHORIZING');

CREATE TABLE mock_wallet_operations (
	operation_id VARCHAR NOT NULL,
	wallet_id VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	result JSON NOT NULL,
	PRIMARY KEY (operation_id),
	FOREIGN KEY(wallet_id) REFERENCES mock_wallets (wallet_id)
);

CREATE TABLE payments (
	payment_id VARCHAR NOT NULL,
	checkout_id VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	customer_id VARCHAR NOT NULL,
	authorization_status VARCHAR(10) NOT NULL,
	asset_reservation_status VARCHAR(11) NOT NULL,
	prefunding_status VARCHAR(11) NOT NULL,
	liquidation_status VARCHAR(11) NOT NULL,
	reimbursement_status VARCHAR(11) NOT NULL,
	settlement_status VARCHAR(17) NOT NULL,
	source VARCHAR NOT NULL,
	engine_payment_id VARCHAR,
	engine_version INTEGER NOT NULL,
	engine_detail JSON NOT NULL,
	reason_code VARCHAR,
	last_error VARCHAR,
	lease_until DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	authorized_at DATETIME,
	settled_at DATETIME,
	PRIMARY KEY (payment_id),
	CHECK (idempotency_key = checkout_id),
	CHECK (source IN ('simulated', 'external')),
	CHECK (authorization_status = 'AUTHORIZED' OR settlement_status = 'NOT_STARTED'),
	UNIQUE (checkout_id),
	FOREIGN KEY(checkout_id) REFERENCES checkouts (checkout_id),
	UNIQUE (idempotency_key),
	FOREIGN KEY(customer_id) REFERENCES customers (customer_id),
	CONSTRAINT authorizationstatus CHECK (authorization_status IN ('PENDING', 'UNKNOWN', 'AUTHORIZED', 'DECLINED')),
	CONSTRAINT assetreservationstatus CHECK (asset_reservation_status IN ('NOT_STARTED', 'PENDING', 'RESERVED', 'FAILED', 'RELEASED')),
	CONSTRAINT prefundingstatus CHECK (prefunding_status IN ('NOT_STARTED', 'PENDING', 'RESERVED', 'FAILED')),
	CONSTRAINT liquidationstatus CHECK (liquidation_status IN ('NOT_STARTED', 'PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')),
	CONSTRAINT reimbursementstatus CHECK (reimbursement_status IN ('NOT_STARTED', 'PENDING', 'COMPLETED', 'FAILED')),
	CONSTRAINT settlementstatus CHECK (settlement_status IN ('NOT_STARTED', 'PENDING', 'COMPLETED', 'RECOVERY_REQUIRED')),
	UNIQUE (engine_payment_id)
);

CREATE INDEX payments_reconciliation ON payments (settlement_status, lease_until);

CREATE TABLE events (
	event_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	event_type VARCHAR NOT NULL,
	checkout_id VARCHAR,
	payment_id VARCHAR,
	source VARCHAR NOT NULL,
	occurred_at DATETIME NOT NULL,
	payload JSON NOT NULL,
	CHECK (source IN ('simulated', 'external')),
	FOREIGN KEY(checkout_id) REFERENCES checkouts (checkout_id),
	FOREIGN KEY(payment_id) REFERENCES payments (payment_id)
);

CREATE INDEX events_checkout_cursor ON events (checkout_id, event_id);

CREATE TABLE tap_events (
	client_event_id VARCHAR NOT NULL,
	payload_fingerprint VARCHAR NOT NULL,
	checkout_id VARCHAR NOT NULL,
	payment_id VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (client_event_id),
	FOREIGN KEY(checkout_id) REFERENCES checkouts (checkout_id),
	FOREIGN KEY(payment_id) REFERENCES payments (payment_id)
);
