from contextlib import asynccontextmanager

EVENTS = []


@asynccontextmanager
async def lifespan():
    EVENTS.append("startup")
    try:
        yield
    finally:
        EVENTS.append("shutdown")


async def status(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"catalog ready"})
