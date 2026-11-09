"""가맹점 원화 송금 (회사 계좌 → 가맹점 계좌).

승인 직후 비동기로 보낸다. 결제 1건당 1회만 (payment_id 로 멱등).
- sim         : 즉시 성공 (데모)
- openbanking : 금융결제원 오픈뱅킹 '입금이체' API.
                ⚠️ 이용기관 계약·출금이체 동의·약정계좌가 필요하고, 필드는 최신 명세로 다시 확인할 것.
실서비스에서는 건별 송금 대신 PG/정산대행(일 단위 정산)으로 묶는 경우가 많다. 어댑터만 바꾸면 된다.
"""
from __future__ import annotations

import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .config import settings

KST = timezone(timedelta(hours=9))


@dataclass
class PayoutResult:
    ok: bool
    bank_tran_id: str | None = None
    error: str | None = None
    retryable: bool = True


class SimBank:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail_next = 0

    async def send(self, payout_id: str, bank_code: str, account: str, holder: str, amount: int, memo: str) -> PayoutResult:
        if self.fail_next:
            self.fail_next -= 1
            return PayoutResult(False, error="sim bank timeout")
        tid = f"SIM{payout_id[-12:]}"
        self.sent.append({"id": payout_id, "bank": bank_code, "account": account, "amount": amount, "memo": memo})
        return PayoutResult(True, tid)


class OpenBankingBank:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=settings.openbanking_base, timeout=15)

    def _bank_tran_id(self) -> str:
        # 이용기관코드(10) + 'U' + 9자리 (하루 동안 유일)
        return settings.openbanking_client_code + "U" + "".join(random.choices(string.digits, k=9))

    async def send(self, payout_id: str, bank_code: str, account: str, holder: str, amount: int, memo: str) -> PayoutResult:
        now = datetime.now(KST).strftime("%Y%m%d%H%M%S")
        bank_tran_id = self._bank_tran_id()
        body = {
            "cntr_account_type": "N", "cntr_account_num": "",          # 약정 계좌 (계약 시 확정)
            "wd_pass_phrase": "NONE", "wd_print_content": memo[:7],
            "name_check_option": "on", "tran_dtime": now, "req_cnt": "1",
            "req_list": [{
                "tran_no": "1", "bank_tran_id": bank_tran_id,
                "bank_code_std": bank_code, "account_num": account, "account_holder_name": holder,
                "print_content": memo[:7], "tran_amt": str(amount),
                "req_client_name": "STOCKBUCKS", "req_client_num": payout_id[-20:], "transfer_purpose": "TR",
            }],
        }
        try:
            r = await self.client.post("/v2.0/transfer/deposit/acnt_num", json=body,
                                       headers={"Authorization": f"Bearer {settings.openbanking_token}"})
            d = r.json()
        except Exception as e:  # noqa: BLE001
            return PayoutResult(False, error=repr(e)[:200], retryable=True)
        if d.get("rsp_code") == "A0000":
            res = (d.get("res_list") or [{}])[0]
            if res.get("bank_rsp_code") in ("000", None):
                return PayoutResult(True, bank_tran_id)
            return PayoutResult(False, error=f"bank {res.get('bank_rsp_code')} {res.get('bank_rsp_message')}", retryable=False)
        # A0007 등 처리중/타임아웃 계열은 같은 bank_tran_id 로 결과조회 후 재시도해야 한다 (이중 송금 방지)
        return PayoutResult(False, error=f"{d.get('rsp_code')} {d.get('rsp_message')}", retryable=d.get("rsp_code") in ("A0007",))


def make_bank():
    return OpenBankingBank() if settings.bank_provider == "openbanking" else SimBank()
