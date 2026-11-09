"""Hand-authored demo snapshots/rankings; not the external v5 optimizer."""
ASSETS = ("BTC", "ETH", "USDC", "USDT")
STABLECOINS = {"USDC", "USDT"}
USDC_KRW = 1400  # Fixed presentation quote, never a market price.
COMPANY_CAPACITY_KRW = 1_000_000
DEMO_CARDS = (
    ("demo-alice", "success"), ("demo-declined", "decline"),
    ("demo-payout-fail", "payout_fail"), ("demo-liquidation-fail", "liquidation_fail"),
    ("demo-reimbursement-fail", "reimbursement_fail"),
    ("demo-wallet-busy", "wallet_busy"), ("demo-execution-fail", "execution_fail"),
    ("demo-prefund-fail", "prefund_fail"), ("demo-hold-expiry", "hold_expiry"),
    ("demo-slippage-fail", "slippage_fail"), ("demo-alternate", "alternate"),
    ("demo-unknown", "unknown"), ("demo-authorization-unknown", "authorization_unknown"),
    ("demo-usdt", "usdt"),
)


def holdings(scenario):
    if scenario == "decline":
        return {"BTC": 100, "ETH": 200, "USDC": 300, "USDT": 400}
    if scenario == "execution_fail":
        return {"BTC": 12000, "ETH": 0, "USDC": 0, "USDT": 0}
    if scenario == "usdt":
        return {"BTC": 0, "ETH": 0, "USDC": 0, "USDT": 100000}
    return {"BTC": 30000, "ETH": 40000, "USDC": 20000, "USDT": 10000}


# Scores and order are illustrative fixture data, not computed financial scores.
RANKINGS = [
    {"asset": "BTC", "rank": 1, "fixture_score": "0.94"},
    {"asset": "ETH", "rank": 2, "fixture_score": "0.88"},
    {"asset": "USDC", "rank": 3, "fixture_score": "0.76"},
    {"asset": "USDT", "rank": 4, "fixture_score": "0.74"},
]


def routes(scenario):
    def route(asset, outcome="SUCCESS", retries=0, slippage="0.001"):
        return {"asset": asset, "route_id": f"fixture-{asset.lower()}-usdc",
                "outcome": outcome, "max_retries": retries, "slippage": slippage}
    if scenario == "slippage_fail":
        return [route("BTC", "FAILED", 2, "0.008")]
    if scenario == "liquidation_fail":
        return [route("BTC", "FAILED"), route("ETH", "FAILED", 1)]
    if scenario == "alternate":
        return [route("BTC", "FAILED", slippage="0.008"), route("ETH")]
    if scenario == "unknown":
        return [route("BTC", "UNKNOWN")]
    return [route(asset) for asset in ASSETS]
