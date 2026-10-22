"""Offline regression checks; not part of the 50 live API scenarios."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from reporting import HtmlReport


class ReportingChecks(unittest.TestCase):
    def test_phase_statuses(self):
        for phases, expected in (
            ({}, "notrun"),
            ({"setup": "passed", "call": "passed"}, "notrun"),
            ({"setup": "failed"}, "error"),
            ({"call": "passed", "teardown": "failed"}, "error"),
            ({"call": "failed", "teardown": "passed"}, "failed"),
            ({"setup": "skipped", "teardown": "passed"}, "skipped"),
            ({"call": "passed", "teardown": "passed"}, "passed"),
        ):
            with self.subTest(phases=phases):
                case = {"phases": {name: {"outcome": value} for name, value in phases.items()}}
                self.assertEqual(HtmlReport.status(case), expected)

    def test_escapes_model_text_and_masks_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "report.html"
            report = HtmlReport(path, "http://localhost:8080")
            item = SimpleNamespace(nodeid="test_21_sample", name="test_21_sample")
            report.collect([item])
            api = SimpleNamespace(_report_secrets={"private-cookie"}, trace=[{
                "text": "<script>alert('unsafe')</script> private-cookie",
                "job_id": "job-123", "status": "succeeded", "result": {"answer": "Готово"},
            }])
            report.record(item, SimpleNamespace(when="call", outcome="failed", duration=1,
                                                longrepr="private-cookie <img onerror=alert(1)>"), api)
            report.write(1)
            content = path.read_text()
            self.assertTrue(report.written)
            self.assertNotIn("private-cookie", content)
            self.assertNotIn("<script>alert", content)
            self.assertNotIn("<img", content)
            self.assertIn("&lt;script&gt;", content)
            self.assertIn("&lt;REDACTED&gt;", content)
            self.assertIn('data-status="failed"', content)
            self.assertIn("job-123", content)
            self.assertEqual(content.count("<script>"), 1)

    def test_malformed_result_and_collection_errors_still_render(self):
        with tempfile.TemporaryDirectory() as folder:
            report = HtmlReport(Path(folder) / "report.html", "http://localhost:8080")
            item = SimpleNamespace(nodeid="test_broken", name="test_broken")
            report.collect([item])
            api = SimpleNamespace(_report_secrets=set(), trace=[{
                "text": "Запрос", "job_id": 3, "status": 5, "result": "invalid envelope",
            }])
            report.record(item, SimpleNamespace(when="setup", outcome="failed", duration=0.1,
                                                longrepr="Invalid API response"), api)
            report.collection_errors.append("SyntaxError: <bad>")
            report.write(2)
            content = report.path.read_text()
            self.assertIn('data-status="error"', content)
            self.assertIn("Ошибки сборки тестов", content)
            self.assertIn("SyntaxError: &lt;bad&gt;", content)
            self.assertIn("Ответ не получен", content)


if __name__ == "__main__":
    unittest.main()
