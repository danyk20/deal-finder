"""FastAPI application: JSON API (/api) + web UI (/). Hosts the scheduler in its lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .scheduler import shutdown_scheduler, start_scheduler
from .web.api import router as api_router
from .web.routes import router as web_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(title="Deal Finder", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
app.include_router(web_router)


def main() -> None:
    import uvicorn

    uvicorn.run("deal_finder.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
