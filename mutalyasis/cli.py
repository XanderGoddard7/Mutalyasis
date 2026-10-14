from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import analyze, write_json_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutalyasis",
        description="Measure how effectively Python tests detect small code changes.",
    )
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        default=Path("."),
        help="project directory to analyze (default: current directory)",
    )
    parser.add_argument(
        "--tests",
        default="tests",
        help="test directory relative to the project (default: tests)",
    )
    parser.add_argument(
        "--test-command",
        default="{python} -m unittest discover -s {tests}",
        help="test command; supports {python}, {project}, and {tests} placeholders",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="seconds allowed for each test run (default: 30)",
    )
    parser.add_argument(
        "--max-mutants",
        type=int,
        help="analyze at most this many mutants",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="write the complete results to this JSON file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if arguments.max_mutants is not None and arguments.max_mutants <= 0:
        parser.error("--max-mutants must be greater than zero")

    try:
        summary = analyze(
            project_path=arguments.project,
            tests=arguments.tests,
            test_command=arguments.test_command,
            timeout=arguments.timeout,
            max_mutants=arguments.max_mutants,
        )
        if arguments.json_report is not None:
            write_json_report(arguments.json_report, summary)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"mutalyasis: error: {error}", file=sys.stderr)
        return 2

    for result in summary.results:
        print(
            f"{result.status.upper():9} {result.file}:{result.line} "
            f"[{result.operator}]"
        )
    print(
        f"\nMutation score: {summary.score:.1f}% "
        f"({summary.killed + summary.timed_out}/{summary.total} killed)"
    )
    print(
        f"Killed: {summary.killed}  Survived: {summary.survived}  "
        f"Timed out: {summary.timed_out}"
    )
    if arguments.json_report is not None:
        print(f"JSON report: {arguments.json_report}")
    return 0 if summary.survived == 0 else 1
