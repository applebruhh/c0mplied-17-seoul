"""Read-only transport for engine Ops, whose JSON schemas are unspecified."""
from urllib.parse import quote

import httpx


class OperationsEngineUnavailable(RuntimeError):
    pass


class OperationsEngineAdapter:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def _get(self, path: str, **filters):
        try:
            response = await self._client.get(
                path, params={key: value for key, value in filters.items() if value is not None},
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise OperationsEngineUnavailable from None

    async def payments(self, limit: int):
        return await self._get("/ops/payments", limit=limit)

    async def payment(self, pid: str):
        return await self._get(f"/ops/payments/{quote(pid, safe='')}")

    async def alerts(self, limit: int, audience: str | None):
        return await self._get("/ops/alerts", limit=limit, audience=audience)

    async def decisions(self, limit: int, kind: str | None, ref_id: str | None):
        return await self._get("/ops/decisions", limit=limit, kind=kind, ref_id=ref_id)
