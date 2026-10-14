from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from mutalyasis.cli import main
from mutalyasis.core import (
    AnalysisSummary,
    MutationResult,
    _run_tests,
    analyze,
    apply_mutation,
    collect_mutations,
    discover_source_files,
    write_json_report,
)


class MutationCollectionTests(unittest.TestCase):
    def test_collects_arithmetic_comparison_boolean_and_literal_mutations(self) -> None:
        source = "def check(x):\n    return x + 2 > 3 and True\n"

        mutations = collect_mutations(source)

        self.assertEqual(
            [mutation.operator for mutation in mutations],
            ["And", "Gt", "Add", "Integer +1", "Integer -1", "Integer +1", "Integer -1", "Boolean"],
        )
        addition_mutation = next(mutation for mutation in mutations if mutation.operator == "Add")
        self.assertEqual(
            apply_mutation(source, addition_mutation),
            "def check(x):\n    return x - 2 > 3 and True\n",
        )

    def test_handles_non_ascii_text_before_mutated_expression(self) -> None:
        source = "# café\nvalue = 1 + 2\n"

        mutation = collect_mutations(source)[0]

        self.assertEqual(apply_mutation(source, mutation), "# café\nvalue = 1 - 2\n")
        self.assertEqual(mutation.line, 2)

    def test_reports_invalid_python(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot parse Python source"):
            collect_mutations("def broken(:\n")

    def test_ignores_test_and_generated_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n")
            (root / "test_app.py").write_text("value = 1\n")
            (root / "tests").mkdir()
            (root / "tests" / "helper.py").write_text("value = 1\n")
            (root / ".venv").mkdir()
            (root / ".venv" / "site.py").write_text("value = 1\n")

            self.assertEqual(
                [path.relative_to(root).as_posix() for path in discover_source_files(root)],
                ["app.py"],
            )


class MutationAnalysisTests(unittest.TestCase):
    def test_rejects_missing_test_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "app.py").write_text("value = 1 + 2\n")
            with self.assertRaisesRegex(ValueError, "test directory does not exist"):
                analyze(Path(directory))

    def test_reports_test_command_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "tests").mkdir()

            status, _ = _run_tests(
                project,
                "tests",
                "{python} -c 'import time; time.sleep(1)'",
                timeout=0.01,
            )

            self.assertEqual(status, "timed_out")

    def test_runs_baseline_and_mutants_without_changing_original_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            source = root / "calculator.py"
            source.write_text("def add(a, b):\n    return a + b\n")
            (root / "tests" / "test_calculator.py").write_text(
                "import unittest\n"
                "from calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n"
            )
            original = source.read_text()

            summary = analyze(root, timeout=10)

            self.assertEqual(summary.total, 1)
            self.assertEqual(summary.killed, 1)
            self.assertEqual(summary.survived, 0)
            self.assertEqual(summary.score, 100.0)
            self.assertEqual(source.read_text(), original)
            self.assertEqual(summary.results[0].status, "killed")

            report = root / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main([str(root), "--json-report", str(report)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(report.read_text())["mutation_score"], 100.0)

    def test_fails_fast_when_baseline_tests_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "app.py").write_text("value = 1 + 2\n")
            (root / "tests" / "test_app.py").write_text(
                "import unittest\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        self.fail('baseline failure')\n"
            )

            with self.assertRaisesRegex(RuntimeError, "baseline tests did not pass"):
                analyze(root, timeout=10)

    def test_writes_machine_readable_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            summary = AnalysisSummary(
                total=1,
                killed=1,
                survived=0,
                timed_out=0,
                results=[MutationResult("app.py", 1, "Add", "killed")],
            )

            write_json_report(report, summary)

            self.assertEqual(json.loads(report.read_text())["mutation_score"], 100.0)


if __name__ == "__main__":
    unittest.main()
