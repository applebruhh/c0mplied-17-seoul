"""회사 돈으로 가맹점에 원화 송금. 승인(AUTHORIZED) 직후 비동기, 결제당 1회 (payment_id 유니크)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from .. import runtime
from ..db import SessionLocal
from ..models import Merchant, MerchantPayout, Payment
from ..states import Ev, PayoutStatus
from .common import alert, event

MAX_ATTEMPTS = 4


async def run_payout(payment_id: str) -> None:
    s = SessionLocal()
    try:
        p = s.get(Payment, payment_id)
        if p is None or p.auth_status != "AUTHORIZED":
            return
        po = s.query(MerchantPayout).filter_by(payment_id=payment_id).one_or_none()
        if po is None:
            po = MerchantPayout(payment_id=payment_id, merchant_id=p.merchant_id, amount_krw=p.amount_krw)
            s.add(po)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                return                          # 다른 워커가 이미 처리 중
        if po.status == PayoutStatus.SENT.value:
            return
        m = s.get(Merchant, p.merchant_id)
        if m is None:
            po.status, po.error = PayoutStatus.FAILED.value, "unknown merchant"
            p.payout_status = po.status
            alert(s, "critical", f"가맹점 정보 없음 {p.merchant_id}: 원화 송금 불가", payment_id)
            s.commit()
            return

        while po.attempts < MAX_ATTEMPTS:
            po.attempts += 1
            s.commit()
            res = await runtime.rt.bank.send(po.id, m.bank_code, m.account_num, m.account_holder,
                                             int(round(po.amount_krw)), f"SB{payment_id[-5:]}")
            if res.ok:
                po.status, po.bank_tran_id, po.sent_at = PayoutStatus.SENT.value, res.bank_tran_id, datetime.now(timezone.utc)
                p.payout_status = po.status
                event(s, payment_id, Ev.MERCHANT_PAYOUT_SENT, amount_krw=po.amount_krw, bank_tran_id=res.bank_tran_id)
                s.commit()
                return
            po.error = res.error
            s.commit()
            if not res.retryable:
                break
            await asyncio.sleep(min(2 ** po.attempts * 0.2, 5))
        po.status = PayoutStatus.FAILED.value
        p.payout_status = po.status
        event(s, payment_id, Ev.MERCHANT_PAYOUT_FAILED, error=po.error)
        alert(s, "critical", f"가맹점 원화 송금 실패 ({po.error}). 수동 송금 필요", payment_id)
        s.commit()
    finally:
        s.close()
