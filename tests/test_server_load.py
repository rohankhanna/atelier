"""Load/concurrency tests: /health must stay responsive under streaming load.

Regression guard for the event-loop wedge where a synchronous, fully-buffering
upstream call inside an async handler froze the single-worker event loop for the
entire duration of a stream, starving /health (and every other request) until a
restart. The fix streams upstream chunk-by-chunk on an async client so the loop
is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

import atelier.server as server_mod
from atelier.oauth_custody import AccessGrant
from atelier.server import create_app
from atelier.standin import StandInTokenLedger

N_CHUNKS = 10
CHUNK_DELAY = 0.05  # seconds between upstream chunks
STREAM_DURATION = N_CHUNKS * CHUNK_DELAY


class FakeCustody:
    async def access_grant(self, account: str) -> AccessGrant:
        return AccessGrant(
            access_token="PROVIDER_ACCESS_TOKEN",
            account_id=account,
            expires_at=5000,
        )


class _SlowUpstreamStream:
    """Models a long-running SSE upstream: chunks trickle in over time.

    Each chunk is preceded by an ``await asyncio.sleep`` (cooperative), so a
    correctly-written handler yields the event loop between chunks. If the
    handler blocked the loop (the old bug), /health would stall for the full
    STREAM_DURATION.
    """

    @property
    def status_code(self) -> int:
        return 200

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": "text/event-stream"}

    async def aiter_bytes(self):
        for i in range(N_CHUNKS):
            await asyncio.sleep(CHUNK_DELAY)
            yield f"data: chunk-{i}\n\n".encode()

    async def aclose(self) -> None:
        return None


class SlowStreamTransport:
    def __init__(self, client) -> None:
        self.client = client

    async def open_stream(self, request) -> _SlowUpstreamStream:
        return _SlowUpstreamStream()


def _make_app():
    ledger = StandInTokenLedger(now=lambda: 1000)
    custody = FakeCustody()
    return create_app(custody=custody, ledger=ledger)


async def _mint_token(client: httpx.AsyncClient) -> str:
    resp = await client.post("/v1/standin", json={"account": "test-account"})
    return resp.json()["token"]


_STREAM_BODY = {
    "url": "https://upstream.example/responses",
    "method": "POST",
    "headers": {},
    "body_b64": "",
}


@pytest.mark.anyio
async def test_health_stays_responsive_under_concurrent_streams(monkeypatch):
    """N concurrent in-flight streams must not push /health latency over 500ms."""
    monkeypatch.setattr(server_mod, "_HttpTransport", SlowStreamTransport)
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _mint_token(client)
        auth = {"Authorization": f"Bearer {token}"}

        n_streams = 10

        async def run_stream() -> httpx.Response:
            return await client.post("/v1/proxy/stream", json=_STREAM_BODY, headers=auth)

        stream_tasks = [asyncio.create_task(run_stream()) for _ in range(n_streams)]

        # Let the streams get in flight, then hammer /health while they run.
        await asyncio.sleep(CHUNK_DELAY)
        latencies: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            health = await client.get("/health")
            latencies.append(time.perf_counter() - t0)
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            await asyncio.sleep(0.01)

        responses = await asyncio.gather(*stream_tasks)

    assert max(latencies) < 0.5, f"/health stalled under load: max={max(latencies):.3f}s"
    for resp in responses:
        assert resp.status_code == 200
        assert resp.headers["x-atelier-upstream-status"] == "200"
        body = resp.text
        assert "chunk-0" in body
        assert f"chunk-{N_CHUNKS - 1}" in body


async def _drive_stream_asgi(app, *, path: str, headers: dict[str, str], body: bytes):
    """Drive an ASGI app directly and record (timestamp, message) for every event.

    httpx.ASGITransport buffers the whole response before returning, so we talk the
    ASGI protocol directly to observe when each response-body chunk is actually
    emitted by the app. A truly-streaming handler emits body events as its
    generator yields; a buffering one emits them all at once at the end.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }
    delivered = False
    idle = asyncio.Event()  # never set; parks receive() once the body is delivered

    async def receive():
        nonlocal delivered
        if delivered:
            # Suspend (cancellably) so the response streamer can run to completion;
            # Starlette cancels this once stream_response finishes.
            await idle.wait()
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    events: list[tuple[float, dict]] = []

    async def send(message):
        events.append((time.perf_counter(), message))

    await app(scope, receive, send)
    return events


@pytest.mark.anyio
async def test_stream_emits_chunks_incrementally(monkeypatch):
    """The handler must emit body chunks as they arrive, not buffer the whole body."""
    monkeypatch.setattr(server_mod, "_HttpTransport", SlowStreamTransport)
    ledger = StandInTokenLedger(now=lambda: 1000)
    app = create_app(custody=FakeCustody(), ledger=ledger)
    token = ledger.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=300,
    ).token

    start = time.perf_counter()
    events = await _drive_stream_asgi(
        app,
        path="/v1/proxy/stream",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        body=json.dumps(_STREAM_BODY).encode(),
    )

    start_events = [m for _, m in events if m["type"] == "http.response.start"]
    body_times = [
        t - start
        for t, m in events
        if m["type"] == "http.response.body" and m.get("body")
    ]

    assert start_events and start_events[0]["status"] == 200
    assert len(body_times) >= 2, "expected multiple incremental body chunks"
    # First chunk arrives near CHUNK_DELAY; a buffering handler would emit nothing
    # until ~STREAM_DURATION.
    assert body_times[0] < STREAM_DURATION / 2, (
        f"first chunk at {body_times[0]:.3f}s suggests buffering, not streaming "
        f"(stream duration ~{STREAM_DURATION:.3f}s)"
    )
    # Chunks are spread out over the stream's lifetime, not bunched at the end.
    assert body_times[-1] - body_times[0] > STREAM_DURATION / 2
