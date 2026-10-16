"""The Map under /api/v1: every session at a glance, clustered by repository (read-only)."""

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from robin import activity
from robin.service.runtime import ServiceRuntime

MAX_CARD_IDS = 50


def register(app: FastAPI, runtime: ServiceRuntime, authed: Any, api: str) -> None:
    store = runtime.store

    def fresh() -> None:
        activity.catch_up(store, max_batches=2)  # nearly current; the background loop does the rest

    @app.get(f"{api}/map")
    async def snapshot(
        _: str = authed, per_cluster: int = Query(50, ge=1, le=500), cluster: str | None = None
    ) -> dict[str, Any]:
        await asyncio.to_thread(fresh)
        return await asyncio.to_thread(activity.map_snapshot, store, per_cluster, cluster)

    @app.get(f"{api}/map/cards")
    async def cards(session_ids: str, _: str = authed) -> dict[str, Any]:
        ids = [s for s in session_ids.split(",") if s][:MAX_CARD_IDS]
        await asyncio.to_thread(fresh)
        return await asyncio.to_thread(activity.map_cards, store, ids)

    @app.get(f"{api}/sessions/{{session_id}}/activity")
    async def session(session_id: str, _: str = authed) -> dict[str, Any]:
        found = await asyncio.to_thread(activity.session_activity, store, session_id)
        if found is None:
            raise HTTPException(404, "no such session")
        return found
