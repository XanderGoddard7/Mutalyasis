# Mutalyasis

Mutalyasis is a lightweight mutation-testing CLI for Python projects. It makes
small, controlled changes to Python code, runs the project's tests against each
change, and reports whether the tests detect it. Mutants are tested in temporary
copies, so analysis does not modify your project files.

## Run from a checkout

```sh
python -m mutalyasis /path/to/project
```

The default test runner is Python's built-in `unittest`, discovering tests in
`tests/`. Install the project to use the `mutalyasis` command directly:

```sh
python -m pip install .
mutalyasis /path/to/project
```

Use a different test runner by passing a command. The placeholders are replaced
with the active Python executable, the copied project directory, and the tests
directory relative to that copy:

```sh
mutalyasis . --tests tests --test-command "{python} -m pytest -q {tests}"
```

Supported mutations include arithmetic and comparison operators, `and`/`or`,
unary signs and `not`, integer literals, and boolean literals. Test failures
kill a mutant; a passing test run means it survived. Timed-out mutants are
counted as killed and reported separately. The mutation score is the percentage
of mutants killed, including timed-out mutants.

Useful options:

```text
--tests DIR             Test directory relative to the project (default: tests)
--test-command COMMAND  Test command; supports {python}, {project}, and {tests}
--timeout SECONDS       Timeout for each test run (default: 30)
--max-mutants N         Limit the number of generated mutants
--json-report FILE      Save detailed results as JSON
```

The command exits with status `0` when every mutant is killed, `1` when one or
more mutants survive, and `2` for setup, configuration, or baseline-test errors.

## Development

```sh
python -m unittest discover -s tests -v
```
