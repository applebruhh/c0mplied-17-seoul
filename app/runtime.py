"""실행 중 객체 묶음 (체인 어댑터, 은행, 옵티마이저, 백그라운드 태스크)."""
from __future__ import annotations

import asyncio
import logging

from .algo.sell import SellOptimizer
from .bank import make_bank
from .config import settings
from .market.state import market

log = logging.getLogger("runtime")


class Runtime:
    def __init__(self):
        self.market = market
        self.optimizer = SellOptimizer(market)
        self.bank = make_bank()
        if settings.mode == "live":
            from .chain.evm import EvmChain
            self.chain = EvmChain()
        else:
            from .chain.sim import SimChain
            self.chain = SimChain(market)
        self.tasks: set[asyncio.Task] = set()
        self.balance_cache: dict[str, tuple[dict[str, float], float]] = {}

    def spawn(self, coro, name: str = "") -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.add(t)

        def _done(task: asyncio.Task):
            self.tasks.discard(task)
            if not task.cancelled() and task.exception():
                log.exception("task %s failed", name, exc_info=task.exception())
        t.add_done_callback(_done)
        return t

    async def drain(self, timeout: float = 30) -> None:
        """테스트/데모: 진행 중 태스크가 끝날 때까지 기다린다."""
        for _ in range(50):
            pending = [t for t in self.tasks if not t.done()]
            if not pending:
                return
            await asyncio.wait(pending, timeout=timeout)


rt = Runtime()


def reset_runtime() -> Runtime:
    global rt
    rt = Runtime()
    return rt
