from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from backend.yandex_calendar import service


def test_request_spacing_across_threads(monkeypatch):
    now = [0.0]
    starts = []
    monkeypatch.setattr(service.time, "monotonic", lambda: now[0])

    def sleep(delay):
        starts.append(now[0])
        now[0] += delay

    monkeypatch.setattr(service.time, "sleep", sleep)
    limiter = service.RequestRateLimiter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: limiter.wait(), range(20)))
    assert now[0] == pytest.approx(3.8)
    assert starts == pytest.approx([i * 0.2 for i in range(19)])
    # Idle time does not accumulate a burst allowance.
    now[0] += 10
    limiter.wait()
    limiter.wait()
    assert now[0] == pytest.approx(14)


def test_transports_share_limiter_and_validate_before_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(service.request_limiter, "wait", lambda: waits.append(True))
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        service.request(client, "GET", service.ORIGIN)
        service.HTTPXDAVSession(client).request("REPORT", service.ORIGIN)
        assert len(waits) == 2  # Adapter consumes quota only once.

    class Session:
        def request(self, method, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return httpx.Response(200)

    guarded = service.GuardedSession(Session())
    guarded.request("PROPFIND", service.ORIGIN)
    assert len(waits) == 3
    with pytest.raises(ValueError):
        guarded.request("GET", "https://example.com/")
    with pytest.raises(ValueError):
        service.request(None, "GET", "https://example.com/")
    assert len(waits) == 3
