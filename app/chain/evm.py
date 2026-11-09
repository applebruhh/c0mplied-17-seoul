"""실체인 어댑터 (EVM, 기본 Base).

- 잔고     : JSON-RPC eth_getBalance / ERC20 balanceOf
- DEX      : 0x Swap API v2 (allowance-holder). 여러 DEX 를 묶어 가장 좋은 경로를 준다.
- 서명     : 임베디드 지갑
    privy : Privy 서버 지갑 RPC (사용자가 가입 때 위임한 서명 권한으로 서버가 트랜잭션 전송)
    local : 개발/테스트넷용 로컬 개인키 (SB_LOCAL_KEYS='{"0xaddr":"0xkey"}')
- 영수증   : eth_getTransactionReceipt → ERC20 Transfer 로그로 실제 수령량 계산

⚠️ 실서비스 전 확인: Privy 요청 서명 형식, 0x v2 응답 필드, 토큰 주소(assets.py), 가스 정책.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time

import httpx

from ..assets import ASSETS, NATIVE
from ..config import settings
from .base import Quote, Receipt, SwapError, TxResult, WalletRef

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZEROEX = "https://api.0x.org/swap/allowance-holder"
PRIVY = "https://api.privy.io/v1"


def _pad(addr_or_int) -> str:
    if isinstance(addr_or_int, int):
        return f"{addr_or_int:064x}"
    return addr_or_int.lower().replace("0x", "").rjust(64, "0")


def to_base(symbol: str, units: float) -> int:
    return int(round(units * 10 ** ASSETS[symbol].decimals))


def from_base(symbol: str, raw: int) -> float:
    return raw / 10 ** ASSETS[symbol].decimals


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.client = httpx.AsyncClient(timeout=10)
        self._id = 0

    async def call(self, method: str, params: list):
        self._id += 1
        r = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise RuntimeError(f"{method}: {d['error']}")
        return d["result"]


# ── 서명자 (임베디드 지갑) ──────────────────────────────────────
class LocalSigner:
    def __init__(self, rpc: Rpc):
        from eth_account import Account
        self.Account = Account
        self.rpc = rpc
        self.keys = {k.lower(): v for k, v in json.loads(os.getenv("SB_LOCAL_KEYS", "{}")).items()}

    async def send(self, wallet: WalletRef, tx: dict) -> str:
        key = self.keys.get(wallet.address.lower())
        if not key:
            raise SwapError(f"로컬 키 없음: {wallet.address}", retryable=False)
        nonce = int(await self.rpc.call("eth_getTransactionCount", [wallet.address, "pending"]), 16)
        block = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
        base_fee = int(block["baseFeePerGas"], 16)
        tip = int(await self.rpc.call("eth_maxPriorityFeePerGas", []), 16)
        full = {"chainId": settings.chain_id, "nonce": nonce, "to": tx["to"], "data": tx.get("data", "0x"),
                "value": int(tx.get("value", 0) or 0), "maxPriorityFeePerGas": tip, "maxFeePerGas": base_fee * 2 + tip}
        if tx.get("gas"):
            full["gas"] = int(tx["gas"])
        else:
            est = await self.rpc.call("eth_estimateGas", [{"from": wallet.address, "to": full["to"],
                                                           "data": full["data"], "value": hex(full["value"])}])
            full["gas"] = int(int(est, 16) * 1.2)
        signed = self.Account.sign_transaction(full, key)
        raw = signed.raw_transaction.hex()
        return await self.rpc.call("eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])


class PrivySigner:
    """Privy 서버 지갑. 가입 시 사용자가 '매도 위임'에 동의하면 서버 서명 권한(authorization key)이 붙는다."""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15)
        cred = f"{settings.privy_app_id}:{settings.privy_app_secret}".encode()
        self.auth = "Basic " + base64.b64encode(cred).decode()

    def _auth_signature(self, url: str, body: dict) -> str | None:
        if not settings.privy_auth_key:
            return None
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        payload = {"version": 1, "method": "POST", "url": url, "body": body,
                   "headers": {"privy-app-id": settings.privy_app_id}}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        pem = settings.privy_auth_key.replace("wallet-auth:", "")
        der = base64.b64decode(pem)
        key = serialization.load_der_private_key(der, password=None)
        sig = key.sign(canonical, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(sig).decode()

    async def send(self, wallet: WalletRef, tx: dict) -> str:
        url = f"{PRIVY}/wallets/{wallet.provider_wallet_id}/rpc"
        body = {"method": "eth_sendTransaction", "caip2": f"eip155:{settings.chain_id}",
                "params": {"transaction": {"to": tx["to"], "data": tx.get("data", "0x"),
                                           "value": hex(int(tx.get("value", 0) or 0))}}}
        headers = {"privy-app-id": settings.privy_app_id, "Authorization": self.auth}
        sig = self._auth_signature(url, body)
        if sig:
            headers["privy-authorization-signature"] = sig
        r = await self.client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise SwapError(f"Privy {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        return r.json()["data"]["hash"]


# ── 체인 어댑터 ────────────────────────────────────────────────
class EvmChain:
    def __init__(self):
        self.rpc = Rpc(settings.rpc_url)
        self.http = httpx.AsyncClient(timeout=10)
        self.signers = {"local": LocalSigner(self.rpc), "privy": PrivySigner()}

    def _signer(self, wallet: WalletRef):
        return self.signers[wallet.provider]

    async def balances(self, wallet: WalletRef) -> dict[str, float]:
        out: dict[str, float] = {}

        async def one(sym: str):
            a = ASSETS[sym]
            if a.address == NATIVE:
                raw = int(await self.rpc.call("eth_getBalance", [wallet.address, "latest"]), 16)
            else:
                res = await self.rpc.call("eth_call", [{"to": a.address, "data": "0x70a08231" + _pad(wallet.address)}, "latest"])
                raw = int(res, 16) if res and res != "0x" else 0
            if raw:
                out[sym] = from_base(sym, raw)

        await asyncio.gather(*(one(s) for s, a in ASSETS.items() if a.address))
        return out

    def _zx_headers(self):
        return {"0x-api-key": settings.zeroex_api_key, "0x-version": "v2"}

    async def quote(self, wallet: WalletRef, sell: str, buy: str, sell_units: float, slippage: float) -> Quote:
        params = {"chainId": settings.chain_id, "sellToken": ASSETS[sell].address, "buyToken": ASSETS[buy].address,
                  "sellAmount": str(to_base(sell, sell_units)), "taker": wallet.address,
                  "slippageBps": int(slippage * 10_000)}
        r = await self.http.get(f"{ZEROEX}/quote", params=params, headers=self._zx_headers())
        if r.status_code >= 500:
            raise SwapError(f"0x {r.status_code}")
        d = r.json()
        if r.status_code >= 400 or not d.get("liquidityAvailable", True):
            raise SwapError(f"0x 견적 불가: {str(d)[:200]}", retryable=False)
        buy_units = from_base(buy, int(d["buyAmount"]))
        min_buy = from_base(buy, int(d["minBuyAmount"]))
        return Quote(sell, buy, sell_units, buy_units, min_buy, 0.0, d)

    async def _ensure_allowance(self, wallet: WalletRef, q: Quote) -> None:
        allowance = (q.raw.get("issues") or {}).get("allowance")
        if not allowance:
            return
        spender = allowance["spender"]
        data = "0x095ea7b3" + _pad(spender) + _pad(to_base(q.sell_symbol, q.sell_units))
        h = await self._signer(wallet).send(wallet, {"to": ASSETS[q.sell_symbol].address, "data": data})
        rc = await self.wait(TxResult(h), wallet)
        if not rc.success:
            raise SwapError("approve 실패")

    async def swap(self, wallet: WalletRef, q: Quote) -> TxResult:
        if ASSETS[q.sell_symbol].address != NATIVE:
            await self._ensure_allowance(wallet, q)
        tx = q.raw["transaction"]
        h = await self._signer(wallet).send(wallet, {"to": tx["to"], "data": tx["data"],
                                                     "value": int(tx.get("value", "0")), "gas": tx.get("gas")})
        return TxResult(h)

    async def transfer(self, wallet: WalletRef, symbol: str, units: float, to: str) -> TxResult:
        a = ASSETS[symbol]
        if a.address == NATIVE:
            tx = {"to": to, "value": to_base(symbol, units)}
        else:
            tx = {"to": a.address, "data": "0xa9059cbb" + _pad(to) + _pad(to_base(symbol, units))}
        return TxResult(await self._signer(wallet).send(wallet, tx))

    async def wait(self, tx: TxResult, wallet: WalletRef, out_symbol: str | None = None, timeout: float = 60) -> Receipt:
        t0 = time.time()
        while time.time() - t0 < timeout:
            rc = await self.rpc.call("eth_getTransactionReceipt", [tx.tx_hash])
            if rc:
                if int(rc["status"], 16) != 1:
                    return Receipt(False, error="reverted")
                received: dict[str, float] = {}
                if out_symbol:
                    token = ASSETS[out_symbol].address.lower()
                    me = _pad(wallet.address)
                    for lg in rc["logs"]:
                        if lg["address"].lower() == token and lg["topics"][0] == TRANSFER_TOPIC \
                                and lg["topics"][2].lower().endswith(me[-40:]):
                            received[out_symbol] = received.get(out_symbol, 0) + from_base(out_symbol, int(lg["data"], 16))
                return Receipt(True, received)
            await asyncio.sleep(0.5)
        return Receipt(False, error="receipt timeout")

    async def depth_krw(self, symbol: str, price_usd: float, usdkrw: float) -> float:
        """$10,000 어치 매도 견적의 손실률로 D 추정: 평균 슬리피지 s = x/(2D) → D = x/(2s)."""
        if symbol == "USDC":
            return 1e12                      # USDC 는 그대로 송금 (스왑 없음)
        usd = 10_000.0
        units = usd / price_usd
        params = {"chainId": settings.chain_id, "sellToken": ASSETS[symbol].address,
                  "buyToken": ASSETS["USDC"].address, "sellAmount": str(to_base(symbol, units))}
        r = await self.http.get(f"{ZEROEX}/price", params=params, headers=self._zx_headers())
        r.raise_for_status()
        got_usd = from_base("USDC", int(r.json()["buyAmount"]))
        s = max(1e-5, 1 - got_usd / usd)
        return usd * usdkrw / (2 * s)
