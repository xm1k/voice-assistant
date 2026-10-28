"""Offline regression checks; not part of the live API scenarios."""

import tempfile
import unittest
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reporting import HtmlReport


class ReportingChecks(unittest.TestCase):
    def test_pytest_report_lifecycle_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("conftest.py", "reporting.py", "README.md"):
                shutil.copyfile(Path(__file__).with_name(name), root / name)
            (root / "test_offline.py").write_text("def test_offline():\n    assert True\n")
            command = [sys.executable, "-m", "pytest", str(root),
                       "--base-url", "http://localhost:8080", "-q"]

            def run(*args):
                result = subprocess.run([*command, *args], cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            run("--collect-only")
            self.assertFalse((root / "reports").exists())
            run()
            first = list((root / "reports").glob("report_*.html"))
            self.assertEqual(len(first), 1)
            original = first[0].read_bytes()
            run()
            self.assertEqual(len(list((root / "reports").glob("report_*.html"))), 2)
            self.assertEqual(first[0].read_bytes(), original)
            custom = root / "custom.html"
            run("--report-path", str(custom))
            self.assertIn('data-status="passed"', custom.read_text())

    def test_default_filename_uses_moscow_start_time_and_commit(self):
        report = HtmlReport(None, "http://localhost:8080")
        root = Path(__file__).resolve().parent
        revision = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=root, text=True).strip()
        if subprocess.run(["git", "diff-index", "--quiet", "HEAD", "--"], cwd=root).returncode:
            revision += "-dirty"
        self.assertEqual(str(report.started.tzinfo), "Europe/Moscow")
        self.assertEqual(report.path.name, f"report_{report.started:%Y-%m-%d_%H-%M-%S_%f}_{revision}.html")
        self.assertEqual(report.path.parent, Path(__file__).with_name("reports"))
        self.assertFalse(report.written)

    def test_default_filename_without_git(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            report = HtmlReport(None, "http://localhost:8080")
        self.assertTrue(report.path.name.endswith("_unknown.html"))

    def test_default_filename_when_git_times_out(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 2)):
            report = HtmlReport(None, "http://localhost:8080")
        self.assertTrue(report.path.name.endswith("_unknown.html"))

    def test_explicit_path_is_preserved(self):
        report = HtmlReport("/tmp/custom-report.html", "http://localhost:8080")
        self.assertEqual(report.path, Path("/tmp/custom-report.html"))

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
