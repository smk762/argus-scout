"""HTTP server. Read-only for now: state-changing routes arrive with auth (see the epic)."""

from __future__ import annotations

from fastapi import FastAPI

import argus_scout
from argus_scout.budget import ManualOriginMiddleware

app = FastAPI(title="argus-scout", version=argus_scout.__version__)
app.add_middleware(ManualOriginMiddleware)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": argus_scout.__version__}
