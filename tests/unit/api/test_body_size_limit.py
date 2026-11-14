from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request

from covenantiq.api.errors import register_exception_handlers
from covenantiq.api.limits import BodySizeLimitMiddleware
from covenantiq.api.middleware import RequestContextMiddleware

LIMIT = 1_000_000
CHUNK = 64 * 1024


def build_app(reached: list[int]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, limits={("POST", "/upload"): LIMIT})
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        body = await request.body()
        reached.append(len(body))
        return {"bytes": len(body)}

    @app.post("/other")
    async def other(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    return app


class Source:
    """An upload body that records how much of it the server actually pulled."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while self.sent < self.total:
            size = min(CHUNK, self.total - self.sent)
            self.sent += size
            yield b"x" * size


@pytest.fixture
def reached() -> list[int]:
    return []


@pytest.fixture
async def client(reached: list[int]) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(reached))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_small_bodies_pass(client: httpx.AsyncClient, reached: list[int]) -> None:
    response = await client.post("/upload", content=b"x" * 1000)
    assert response.status_code == 200
    assert reached == [1000]


async def test_declared_oversize_is_refused_before_the_body_is_read(
    client: httpx.AsyncClient, reached: list[int]
) -> None:
    response = await client.post("/upload", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert reached == []
    error = response.json()["error"]
    assert error["code"] == "FILE_TOO_LARGE"
    assert error["details"] == {"max_bytes": LIMIT}
    assert response.headers["X-Request-ID"]


async def test_chunked_oversize_is_cut_off_mid_stream(
    client: httpx.AsyncClient, reached: list[int]
) -> None:
    source = Source(total=50 * LIMIT)  # 50 MB, no Content-Length
    response = await client.post("/upload", content=source)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert reached == []
    # the server stopped reading shortly after the limit instead of consuming all 50 MB
    assert source.sent <= LIMIT + 4 * CHUNK


async def test_a_chunked_body_exactly_at_the_limit_is_accepted(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/upload", content=Source(total=LIMIT))
    assert response.status_code == 200
    assert response.json() == {"bytes": LIMIT}


async def test_other_routes_are_not_limited(client: httpx.AsyncClient) -> None:
    response = await client.post("/other", content=b"x" * (3 * LIMIT))
    assert response.status_code == 200


async def test_other_methods_on_the_limited_path_are_not_limited(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/upload")
    assert response.status_code == 405
