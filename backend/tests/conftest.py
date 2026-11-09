from contextlib import asynccontextmanager

import httpx
import pytest

from app.config import Settings
from app.main import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def running_app(path, *, adapter=None, stage_seconds=100, reset_token="test-reset"):
    app = create_app(settings=Settings(
        database_path=path, demo_reset_token=reset_token,
        mock_stage_seconds=stage_seconds, worker_interval=0.01, engine_timeout=0.5,
    ), payment_adapter=adapter)
    if adapter is not None and hasattr(adapter, "attach"):
        adapter.attach(app.state.database)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield app, client


@pytest.fixture
async def api(tmp_path):
    async with running_app(tmp_path / "pos.sqlite3") as pair:
        yield pair
