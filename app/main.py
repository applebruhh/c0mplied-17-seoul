from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import router
from .config import settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.start_workers:
        from .workers import start_all
        start_all()
    yield
    from . import runtime
    for t in list(runtime.rt.tasks):
        t.cancel()


app = FastAPI(title="Stockbucks Payment Engine", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health():
    from . import runtime
    m = runtime.rt.market
    return {"mode": settings.mode, "prices": len(m.prices), "usdkrw": m.usdkrw.usd}
