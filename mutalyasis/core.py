from __future__ import annotations

import ast
import copy
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path


IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
}

BINOP_REPLACEMENTS: dict[type[ast.operator], tuple[ast.operator, ...]] = {
    ast.Add: (ast.Sub,),
    ast.Sub: (ast.Add,),
    ast.Mult: (ast.Div, ast.FloorDiv),
    ast.Div: (ast.Mult,),
    ast.FloorDiv: (ast.Mult, ast.Div),
    ast.Mod: (ast.Mult,),
}

COMPARE_REPLACEMENTS: dict[type[ast.cmpop], ast.cmpop] = {
    ast.Eq: ast.NotEq(),
    ast.NotEq: ast.Eq(),
    ast.Lt: ast.LtE(),
    ast.LtE: ast.Lt(),
    ast.Gt: ast.GtE(),
    ast.GtE: ast.Gt(),
}


@dataclass(frozen=True)
class Mutation:
    start: int
    end: int
    line: int
    operator: str
    replacement: str


@dataclass
class MutationResult:
    file: str
    line: int
    operator: str
    status: str
    output: str = ""


@dataclass
class AnalysisSummary:
    total: int
    killed: int
    survived: int
    timed_out: int
    results: list[MutationResult]
    baseline_output: str = ""

    @property
    def score(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.killed + self.timed_out) / self.total * 100

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "killed": self.killed,
            "survived": self.survived,
            "timed_out": self.timed_out,
            "mutation_score": round(self.score, 2),
            "results": [asdict(result) for result in self.results],
            "baseline_output": self.baseline_output,
        }


class MutationCollector(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.line_starts = [0]
        for line in self.lines:
            self.line_starts.append(self.line_starts[-1] + len(line))
        self.mutations: list[Mutation] = []

    def _character_offset(self, line: int, byte_column: int) -> int:
        text = self.lines[line - 1]
        character_column = len(text.encode("utf-8")[:byte_column].decode("utf-8"))
        return self.line_starts[line - 1] + character_column

    def _add(self, node: ast.expr, operator: str, replacement: str) -> None:
        if not hasattr(node, "end_lineno") or node.end_lineno is None:
            return
        self.mutations.append(
            Mutation(
                start=self._character_offset(node.lineno, node.col_offset),
                end=self._character_offset(node.end_lineno, node.end_col_offset),
                line=node.lineno,
                operator=operator,
                replacement=replacement,
            )
        )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        for replacement_operator in BINOP_REPLACEMENTS.get(type(node.op), ()):
            mutant = copy.deepcopy(node)
            mutant.op = replacement_operator()
            self._add(node, type(node.op).__name__, ast.unparse(mutant))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for index, operator in enumerate(node.ops):
            replacement_operator = COMPARE_REPLACEMENTS.get(type(operator))
            if replacement_operator is None:
                continue
            mutant = copy.deepcopy(node)
            mutant.ops[index] = replacement_operator
            self._add(node, type(operator).__name__, ast.unparse(mutant))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        replacement_operator: ast.boolop = (
            ast.Or() if isinstance(node.op, ast.And) else ast.And()
        )
        mutant = copy.deepcopy(node)
        mutant.op = replacement_operator
        self._add(node, type(node.op).__name__, ast.unparse(mutant))
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        replacements: dict[type[ast.unaryop], ast.unaryop] = {
            ast.UAdd: ast.USub(),
            ast.USub: ast.UAdd(),
        }
        if isinstance(node.op, ast.Not):
            self._add(node, "Not", ast.unparse(node.operand))
        elif type(node.op) in replacements:
            mutant = copy.deepcopy(node)
            mutant.op = replacements[type(node.op)]
            self._add(node, type(node.op).__name__, ast.unparse(mutant))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool):
            self._add(node, "Boolean", repr(not node.value))
        elif isinstance(node.value, int):
            self._add(node, "Integer +1", repr(node.value + 1))
            self._add(node, "Integer -1", repr(node.value - 1))
        self.generic_visit(node)


def collect_mutations(source: str) -> list[Mutation]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(f"cannot parse Python source: {error}") from error
    collector = MutationCollector(source)
    collector.visit(tree)
    return collector.mutations


def apply_mutation(source: str, mutation: Mutation) -> str:
    return source[: mutation.start] + mutation.replacement + source[mutation.end :]


def discover_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if any(part.lower() in {"test", "tests"} for part in relative.parts[:-1]):
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            continue
        files.append(path)
    return files


def _read_python(path: Path) -> tuple[str, str]:
    with tokenize.open(path) as source_file:
        return source_file.read(), source_file.encoding


def _tail(output: str, limit: int = 4000) -> str:
    return output[-limit:].strip()


def _run_tests(
    project: Path,
    tests: str,
    test_command: str,
    timeout: float,
) -> tuple[str, str]:
    tests_path = (project / tests).resolve()
    try:
        tests_argument = str(tests_path.relative_to(project.resolve()))
    except ValueError as error:
        raise ValueError("--tests must point to a location inside the project") from error
    if not tests_path.is_dir():
        raise ValueError(f"test directory does not exist: {tests}")

    command = test_command.replace("{python}", shlex.quote(sys.executable))
    command = command.replace("{project}", shlex.quote(str(project.resolve())))
    command = command.replace("{tests}", shlex.quote(tests_argument))
    command_arguments = shlex.split(command)
    if not command_arguments:
        return "error", "test command is empty"
    try:
        completed = subprocess.run(
            command_arguments,
            cwd=project,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output_parts = (error.stdout, error.stderr)
        output = "".join(
            part.decode(errors="replace") if isinstance(part, bytes) else part or ""
            for part in output_parts
        )
        return "timed_out", _tail(output)
    except OSError as error:
        return "error", str(error)

    output = _tail(completed.stdout + completed.stderr)
    return ("passed" if completed.returncode == 0 else "failed"), output


def _copy_project(source: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(*IGNORED_DIRECTORIES)
    shutil.copytree(source, destination, ignore=ignored)


def analyze(
    project_path: Path,
    tests: str = "tests",
    test_command: str = "{python} -m unittest discover -s {tests}",
    timeout: float = 30,
    max_mutants: int | None = None,
) -> AnalysisSummary:
    root = project_path.resolve()
    if not root.is_dir():
        raise ValueError(f"project directory does not exist: {root}")

    source_files = discover_source_files(root)
    if not source_files:
        raise ValueError(f"no Python source files found under: {root}")

    candidates: list[tuple[Path, str, str, Mutation]] = []
    for path in source_files:
        source, encoding = _read_python(path)
        for mutation in collect_mutations(source):
            candidates.append((path, source, encoding, mutation))
    if max_mutants is not None:
        candidates = candidates[:max_mutants]
    if not candidates:
        raise ValueError("no supported mutations found in the Python source files")

    with tempfile.TemporaryDirectory(prefix="mutalyasis-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        baseline_project = temporary_root / "baseline"
        _copy_project(root, baseline_project)
        baseline_status, baseline_output = _run_tests(
            baseline_project, tests, test_command, timeout
        )
        if baseline_status != "passed":
            details = baseline_output or f"test command status: {baseline_status}"
            raise RuntimeError(f"baseline tests did not pass:\n{details}")

        results: list[MutationResult] = []
        for index, (original_path, source, encoding, mutation) in enumerate(
            candidates, start=1
        ):
            mutant_project = temporary_root / f"mutant-{index}"
            _copy_project(root, mutant_project)
            mutant_path = mutant_project / original_path.relative_to(root)
            mutant_path.write_text(apply_mutation(source, mutation), encoding=encoding)

            status, output = _run_tests(
                mutant_project, tests, test_command, timeout
            )
            if status == "passed":
                result_status = "survived"
            elif status == "timed_out":
                result_status = "timed_out"
            elif status == "error":
                raise RuntimeError(f"could not execute test command: {output}")
            else:
                result_status = "killed"
            results.append(
                MutationResult(
                    file=original_path.relative_to(root).as_posix(),
                    line=mutation.line,
                    operator=mutation.operator,
                    status=result_status,
                    output=output,
                )
            )

    return AnalysisSummary(
        total=len(results),
        killed=sum(result.status == "killed" for result in results),
        survived=sum(result.status == "survived" for result in results),
        timed_out=sum(result.status == "timed_out" for result in results),
        results=results,
        baseline_output=baseline_output,
    )


def write_json_report(path: Path, summary: AnalysisSummary) -> None:
    path.write_text(
        json.dumps(summary.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
