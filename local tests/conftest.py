import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from reporting import HtmlReport

TZ = ZoneInfo("Europe/Moscow")


def now():
    return datetime.now(TZ)


def pytest_addoption(parser):
    parser.addoption("--base-url", help="HTTP(S) URL or IP:port of the public gateway")
    parser.addoption("--job-timeout", type=float, default=180, help="Seconds per LLM message")
    parser.addoption("--insecure", action="store_true", help="Disable TLS verification for a trusted test server")
    parser.addoption("--report-path", default=str(Path(__file__).with_name("reports") / "latest.html"),
                     help="Output HTML report path (overwritten each run)")


def pytest_configure(config):
    value = config.getoption("--base-url")
    if not value:
        raise pytest.UsageError("--base-url is required, e.g. --base-url 127.0.0.1:8080")
    if "://" not in value:
        value = "http://" + value
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                 and not parsed.username and not parsed.password
                 and not any(c.isspace() for c in value))
        if parsed.netloc.endswith(":") or port == 0:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise pytest.UsageError("--base-url must be an HTTP(S) origin or IP:port (no credentials/path)")
    config._service_url = value.rstrip("/")
    timeout = config.getoption("--job-timeout")
    if not 0 < timeout < float("inf"):
        raise pytest.UsageError("--job-timeout must be positive and finite")
    if getattr(config.option, "numprocesses", 0):
        raise pytest.UsageError("Run these live LLM tests sequentially, without -n")
    config._llm_report = HtmlReport(config.getoption("--report-path"), config._service_url)


class Service:
    def __init__(self, base_url, timeout, *, verify=True):
        self.client = httpx.Client(base_url=base_url, timeout=30, follow_redirects=False, verify=verify)
        self.timeout = timeout
        self.username = "llmtest_" + uuid4().hex
        self.password = uuid4().hex
        self._report_secrets = {self.password}
        self.trace = []
        self.owned = set()
        self.active = set()
        self.submission_uncertain = False
        self.sent_before = self.sent_after = None

    def request(self, method, path, expected=200, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            # Suppress traceback frames: request kwargs can contain login credentials.
            pytest.fail(f"{method} {path}: {type(exc).__name__}", pytrace=False)
        self._report_secrets.update(cookie.value for cookie in self.client.cookies.jar)
        if response.status_code != expected:
            body = "<auth response omitted>" if path.startswith("/api/v1/auth/") else response.text[:2000]
            pytest.fail(f"{method} {path}: expected HTTP {expected}, got {response.status_code}; "
                        + body.replace(self.password, "<REDACTED>"), pytrace=False)
        try:
            return response.json()
        except ValueError:
            pytest.fail(f"{method} {path}: invalid JSON response", pytrace=False)

    def register(self):
        self.request("POST", "/api/v1/auth/register", expected=201, json={
            "username": self.username, "password": self.password, "timezone": "Europe/Moscow",
        })
        self.request("GET", "/api/v1/auth/me")

    def relogin(self):
        self.request("POST", "/api/v1/auth/logout")
        self.client.cookies.clear()
        self.request("POST", "/api/v1/auth/login", json={
            "username": self.username, "password": self.password,
        })

    def pages(self, path, **params):
        items, cursor, seen = [], None, set()
        while True:
            page = self.request("GET", path, params={**params, "limit": 100, **({"cursor": cursor} if cursor else {})})
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                return items
            assert cursor not in seen, "Repeated pagination cursor"
            seen.add(cursor)

    def records(self, **params):
        result = self.pages("/api/v1/records", **params)
        self.owned.update(item["id"] for item in result)
        return result

    def get(self, record_id):
        return self.request("GET", f"/api/v1/records/{record_id}")

    def seed(self, kind, title, **fields):
        record = self.request("POST", "/api/v1/records", headers={"Idempotency-Key": uuid4().hex},
                              json={"kind": kind, "title": title, "timezone": "Europe/Moscow", **fields})
        self.owned.add(record["id"])
        return record

    def submit(self, text, request_id=None):
        self.sent_before = now()
        self.submission_uncertain = True
        result = self.request("POST", "/api/v1/chat/messages", expected=202,
                              json={"text": text, "request_id": request_id or uuid4().hex})
        self.sent_after = now()
        self.active.add(result["job_id"])
        self.submission_uncertain = False
        self.trace.append({"text": text, "job_id": result["job_id"], "status": result["status"],
                           "sent_before": self.sent_before.isoformat(), "sent_after": self.sent_after.isoformat()})
        return result["job_id"]

    def wait(self, job_id):
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"Timed out after {self.timeout}s: job={job_id}"
            job = self.request("GET", f"/api/v1/jobs/{job_id}", timeout=min(30, remaining))
            for entry in self.trace:
                if entry["job_id"] == job_id:
                    entry.update(status=job["status"], result=job.get("result"), error=job.get("error"))
            if job["status"] in ("succeeded", "failed", "cancelled"):
                self.active.discard(job_id)
                assert job["status"] == "succeeded", f"LLM job failed: {job}"
                result = job["result"]
                self.owned.update(card["record"]["id"] for card in result.get("cards", []))
                answer = result.get("answer")
                assert isinstance(answer, str) and answer.strip(), "Empty assistant answer"
                assert not answer.lstrip().startswith(("{", "[", "```")), f"Raw JSON/Markdown response: {answer}"
                assert any("а" <= c.lower() <= "я" for c in answer), f"Expected Russian answer: {answer}"
                return result
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"Timed out after {self.timeout}s: job={job_id}, status={job['status']}"
            time.sleep(min(1, remaining))

    def say(self, text):
        return self.wait(self.submit(text))

    def cleanup(self):
        errors = []
        if self.active or self.submission_uncertain:
            try:
                self.request("DELETE", "/api/v1/chat/messages")
            except (AssertionError, httpx.HTTPError, ValueError, KeyError, pytest.fail.Exception) as exc:
                return [f"Could not cancel outstanding jobs; records left intact: {exc}"]
        try:
            self.records()
        except (AssertionError, httpx.HTTPError, ValueError, KeyError, pytest.fail.Exception) as exc:
            errors.append(f"Could not inventory records: {exc}")
        for record_id in sorted(self.owned):
            try:
                response = self.client.get(f"/api/v1/records/{record_id}")
                if response.status_code == 404:
                    continue
                assert response.status_code == 200, f"GET record: HTTP {response.status_code}"
                self.request("DELETE", f"/api/v1/records/{record_id}",
                             params={"scope": "series", "version": response.json()["version"]},
                             headers={"Idempotency-Key": uuid4().hex})
            except (AssertionError, httpx.HTTPError, ValueError, KeyError, pytest.fail.Exception) as exc:
                errors.append(f"Cleanup record {record_id}: {exc}")
        return errors


@pytest.fixture
def service(request):
    api = Service(request.config._service_url, request.config.getoption("--job-timeout"),
                  verify=not request.config.getoption("--insecure"))
    request.node._llm_service = api
    try:
        api.register()
        yield api
    finally:
        errors = api.cleanup() if api.trace or api.owned or api.submission_uncertain else []
        api.client.close()
        if errors:
            pytest.fail("Cleanup errors:\n" + "\n".join(errors))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    api = getattr(item, "_llm_service", None)
    if report.failed and api is not None:
        report.sections.append(("LLM scenario", json.dumps(api.trace, ensure_ascii=False, indent=2)))
    item.config._llm_report.record(item, report, api)


def pytest_collection_finish(session):
    session.config._llm_report.collect(session.items)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        collector.config._llm_report.collection_errors.append(str(report.longrepr))


def pytest_sessionfinish(session, exitstatus):
    if session.config.option.collectonly:
        return
    reporter = session.config._llm_report
    try:
        reporter.write(exitstatus)
    except OSError as exc:
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal:
            terminal.write_line(f"Не удалось сохранить HTML-отчёт: {exc}", red=True)
        session.exitstatus = pytest.ExitCode.INTERNAL_ERROR


def pytest_terminal_summary(terminalreporter):
    reporter = terminalreporter.config._llm_report
    if reporter.written:
        terminalreporter.write_line(f"HTML-отчёт: {reporter.path.resolve()}")
