"""Top-level test runner: discovers tests, schedules parallel workers,
compares results, and writes reports.
"""
from __future__ import annotations

import argparse
import json
import difflib
import os
import re
import signal
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .connection import ConnSpec
from .errors import MotrError, TestAbort, TestSkip
from .interpreter import Interpreter, Options
from .diagnostics import OutputLimitExceeded, safe_text
from .diagnostics import Journal
from .execution import execute_phase, blocked_result
from .integrity_tracking import IntegrityTracker


@dataclass
class TestCase:
    suite: str
    name: str
    test_file: Path
    result_file: Path
    kind: str = "sql"  # "sql" | "script"
    target_branch: str | None = None  # "main", "4.2-dev", etc.

    @property
    def fullname(self) -> str:
        return f"{self.suite}.{self.name}"


@dataclass
class RunReport:
    case: TestCase
    status: str  # "pass" | "fail" | "skip" | "abort"
    cost_ms: int = 0
    diff: str = ""
    reason: str = ""


_CREATE_DATABASE_RE = re.compile(
    r"(?im)^\s*create\s+database(?:\s+if\s+not\s+exists)?\s+`?([a-zA-Z0-9_]+)"
)

_TARGET_BRANCH_RE = re.compile(
    r"(?m)^#\s*@target-branch:\s*(.+?)\s*$"
)

# Small, versioned selections of existing contracts, not automatic coverage.
RISK_PROFILES = {
    "prepared": (("main", "4.2-dev"), (
        "14_issue_regression.prepared_numeric_variants",
        "14_issue_regression.prepared_pruning",
    )),
    "sql": (("main",), (
        "14_issue_regression.issue_recent_sql_semantics",
        "14_issue_regression.issue_recent_temporal_contracts",
    )),
    "search": (("main",), (
        "14_issue_regression.issue_recent_fulltext_contracts",
        "14_issue_regression.issue_recent_vector_contracts",
    )),
}


def _parse_target_branch(test_file: Path) -> str | None:
    """Parse @target-branch directive from test file.

    Returns the target branch name if found, None otherwise.
    """
    try:
        content = test_file.read_text(errors="ignore")
    except OSError:
        return None
    match = _TARGET_BRANCH_RE.search(content)
    return match.group(1) if match else None


def _database_resources(payload: tuple) -> set[str]:
    """Return global database names created by one SQL test payload.

    Generated cases in different suites often share a short case id and thus
    the same database name (for example ``case_s_3_10``).  Running those cases
    together lets their DROP/CREATE cleanup race.  Script scenarios manage
    their own resources and are left unchanged.
    """
    case_data = payload[0]
    if case_data.get("kind") != "sql":
        return set()
    try:
        sql = Path(case_data["test_file"]).read_text(errors="ignore")
    except OSError:
        return set()
    return {f"database:{name.lower()}"
            for name in _CREATE_DATABASE_RE.findall(sql)}


def _resource_safe_batches(payloads: list[tuple]) -> list[list[tuple]]:
    """Greedily partition payloads so no batch shares a database name."""
    batches: list[list[tuple]] = []
    resources_by_batch: list[set[str]] = []
    for payload in payloads:
        resources = _database_resources(payload)
        for idx, used in enumerate(resources_by_batch):
            if resources.isdisjoint(used):
                batches[idx].append(payload)
                used.update(resources)
                break
        else:
            batches.append([payload])
            resources_by_batch.append(set(resources))
    return batches


def _is_suite_excluded(suite_name: str, exclude_patterns: list[str]) -> bool:
    """Check if suite name matches any exclude pattern (exact match or regex)."""
    for pattern in exclude_patterns:
        # Try exact match first
        if suite_name == pattern:
            return True
        # Try regex match
        try:
            if re.search(pattern, suite_name):
                return True
        except re.error:
            # Invalid regex, treat as literal string
            pass
    return False


def _script_suite_dirs(suites_root: Path, suite: str) -> tuple[Path, Path]:
    """Return script directories in priority order: new root, then legacy."""
    return (
        suites_root.parent / "script" / suite,
        suites_root / "scenarios" / suite,
    )


def _script_result_file(
    script_dir: Path, test_file: Path, current_branch: str | None
) -> Path:
    """Use a branch baseline only when that script explicitly supplies one."""
    default = script_dir / "out" / f"{test_file.stem}.out"
    if current_branch:
        variant = script_dir / "out" / f"{test_file.stem}.{current_branch}.out"
        if variant.is_file():
            return variant
    return default


def _sql_result_file(
    result_dir: Path, test_file: Path, current_branch: str | None
) -> Path:
    """Select an explicitly maintained SQL baseline for the MO branch.

    No variant means the shared contract still applies. In particular, do not
    infer a version from SQL output or silently accept another branch's result.
    """
    default = result_dir / f"{test_file.stem}.result"
    if current_branch:
        variant = result_dir / f"{test_file.stem}.{current_branch}.result"
        if variant.is_file():
            return variant
    return default


def _script_files(suites_root: Path, suite: str, current_branch: str | None = None):
    """Yield each script and baseline once, with the root layout winning."""
    seen: set[str] = set()
    for script_dir in _script_suite_dirs(suites_root, suite):
        if not script_dir.exists():
            continue
        for test_file in sorted(script_dir.iterdir()):
            if (not test_file.is_file()
                    or test_file.suffix not in (".sh", ".py")
                    or test_file.stem in seen):
                continue
            seen.add(test_file.stem)
            yield test_file, _script_result_file(
                script_dir, test_file, current_branch
            )


def discover(suites_root: Path, suites_filter: list[str],
             do_test: str | None, skip_test: str | None,
             exclude_suites: list[str] | None = None,
             current_branch: str | None = None) -> list[TestCase]:
    cases: list[TestCase] = []
    do_re = re.compile(do_test) if do_test else None
    skip_re = re.compile(skip_test) if skip_test else None
    exclude_patterns = exclude_suites or []

    scripts_root = suites_root.parent / "script"
    scenarios_root = suites_root / "scenarios"
    suite_names: set[str] = set()
    if suites_root.exists():
        suite_names.update(
            path.name for path in suites_root.iterdir()
            if path.is_dir() and path.name != "scenarios"
        )
    for root in (scripts_root, scenarios_root):
        if root.exists():
            suite_names.update(
                path.name for path in root.iterdir() if path.is_dir()
            )

    for suite_name in sorted(suite_names):
        # Explicit --suite NAME opts back into an otherwise-excluded suite.
        if _is_suite_excluded(suite_name, exclude_patterns):
            if not (suites_filter and suite_name in suites_filter):
                print(f"[SKIP] Excluded by config: {suite_name}")
                continue
        if suites_filter and suite_name not in suites_filter:
            continue

        suite_dir = suites_root / suite_name
        t_dir = suite_dir / "t"
        r_dir = suite_dir / "r"

        # Scripts shadow same-named SQL placeholders.  New root scripts
        # shadow legacy suites/scenarios implementations.
        scripted: set[str] = set()
        for test_file, result_file in _script_files(
            suites_root, suite_name, current_branch
        ):
            scripted.add(test_file.stem)
            target_branch = _parse_target_branch(test_file)
            case = TestCase(
                suite=suite_name,
                name=test_file.stem,
                test_file=test_file,
                result_file=result_file,
                kind="script",
                target_branch=target_branch,
            )
            if do_re and not do_re.search(case.fullname):
                continue
            if skip_re and skip_re.search(case.fullname):
                continue
            if (current_branch and target_branch
                    and target_branch != current_branch):
                continue
            cases.append(case)

        if t_dir.exists():
            for test_file in sorted(t_dir.iterdir()):
                if (not test_file.is_file()
                        or test_file.suffix != ".test"
                        or test_file.stem in scripted):
                    continue
                target_branch = _parse_target_branch(test_file)
                case = TestCase(
                    suite=suite_name,
                    name=test_file.stem,
                    test_file=test_file,
                    result_file=_sql_result_file(r_dir, test_file, current_branch),
                    kind="sql",
                    target_branch=target_branch,
                )
                if do_re and not do_re.search(case.fullname):
                    continue
                if skip_re and skip_re.search(case.fullname):
                    continue
                if (current_branch and target_branch
                        and target_branch != current_branch):
                    continue
                cases.append(case)

    return cases


def resolve_named(
    suites_root: Path,
    names: list[str],
    current_branch: str | None = None,
) -> list[TestCase]:
    """Resolve names like 'smoke.basic' or 'smoke' into TestCase list."""
    result: list[TestCase] = []
    for name in names:
        if "." in name:
            suite, test = name.split(".", 1)
        else:
            suite, test = name, None

        suite_dir = suites_root / suite
        test_dir = suite_dir / "t"
        result_dir = suite_dir / "r"
        script_files = list(_script_files(suites_root, suite, current_branch))
        if not suite_dir.exists() and not script_files:
            raise MotrError(f"suite not found: {suite}")

        if test is None:
            scripted = {test_file.stem for test_file, _ in script_files}
            for test_file, result_file in script_files:
                result.append(TestCase(
                    suite, test_file.stem, test_file, result_file, "script"
                ))
            if test_dir.exists():
                for test_file in sorted(test_dir.iterdir()):
                    if (test_file.suffix == ".test"
                            and test_file.stem not in scripted):
                        result.append(TestCase(
                            suite,
                            test_file.stem,
                            test_file,
                            _sql_result_file(result_dir, test_file, current_branch),
                            "sql",
                        ))
            continue

        script_match = next(
            ((test_file, result_file)
             for test_file, result_file in script_files
             if test_file.stem == test),
            None,
        )
        if script_match:
            test_file, result_file = script_match
            result.append(TestCase(
                suite, test, test_file, result_file, "script"
            ))
            continue

        test_file = test_dir / f"{test}.test"
        if test_file.exists():
            result.append(TestCase(
                suite,
                test,
                test_file,
                _sql_result_file(result_dir, test_file, current_branch),
                "sql",
            ))
        else:
            raise MotrError(f"test not found: {name}")
    return result


def resolve_risk_profile(
    suites_root: Path, profile: str, branch: str | None,
    exclude_suites: list[str],
) -> list[TestCase]:
    branches, names = RISK_PROFILES[profile]
    if branch not in branches:
        raise MotrError(
            f"risk profile {profile!r} requires an explicit compatible branch "
            f"({', '.join(branches)}); got {branch!r}"
        )
    suites = sorted({name.split(".", 1)[0] for name in names})
    # discover's explicit suite option normally opts back into excluded suites.
    # A profile must not silently override that user configuration.
    if any(_is_suite_excluded(suite, exclude_suites) for suite in suites):
        raise MotrError(f"risk profile {profile!r} contains an excluded suite")
    selector = r"^(?:" + "|".join(re.escape(name) for name in names) + r")$"
    cases = discover(suites_root, suites, selector, None, exclude_suites, branch)
    if sorted(case.fullname for case in cases) != sorted(names):
        raise MotrError(
            f"risk profile {profile!r} selection incomplete: "
            f"expected={list(names)!r}, actual={[c.fullname for c in cases]!r}"
        )
    for case in cases:
        if not case.result_file.is_file():
            raise MotrError(
                f"risk profile {profile!r} missing baseline: {case.result_file}"
            )
    by_name = {case.fullname: case for case in cases}
    return [by_name[name] for name in names]


def risk_profile_passed(cases: list[TestCase], reports: list[dict]) -> bool:
    """Require one terminal pass for each selected contract, without omissions."""
    expected = sorted(case.fullname for case in cases)
    actual = sorted(f"{r.get('suite')}.{r.get('name')}" for r in reports)
    return bool(expected) and actual == expected and all(
        r.get("status") == "pass" for r in reports
    )


# ---------- worker ----------

_TITLE_RE = re.compile(r"^[#\s]*(.+?)\s*$")


def _extract_title(path: Path) -> str:
    """First human-readable comment line in the .test / .py / .sh file.

    For .test files: first `# ...` line (skipping the file's own case-id
    header if it looks like `# NN_xxx -- ID`).
    For .py files: docstring first line.
    """
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return ""
    if path.suffix == ".py":
        m = re.search(r'"""([^"\n]+)', text)
        if m:
            return m.group(1).strip().rstrip(".")
    # .test / .sh: first meaningful comment after optional shebang.
    # Skips boilerplate headers produced by the generator:
    #   `# <plan name> -- <ID>`   (trailing "-- ID" shape)
    #   `# auto-synthesized ...`  /  `# generated from ...`
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#!"):
            continue
        if s.startswith("#"):
            body = s.lstrip("#").strip()
            low = body.lower()
            if re.search(r"--\s*[A-Z]{1,6}-\d+[a-z]?\s*$", body):
                continue
            if low.startswith(("auto-synthesized", "generated from",
                               "pre-clean", "generated ",
                               "testplan", "skip", "see ",
                               "real implementation")):
                continue
            return body
        break
    return ""


def _script_timeout_sec(case: TestCase, run_cfg: dict) -> int:
    env_val = os.environ.get("MOTR_SCRIPT_TIMEOUT", "").strip()
    if env_val.isdigit():
        return max(30, int(env_val))
    suite_map = run_cfg.get("suite_script_timeouts") or {}
    if case.suite in suite_map:
        return max(30, int(suite_map[case.suite]))
    return max(30, int(run_cfg.get("script_timeout_sec", 600)))


def compare_case_output(case: TestCase, expected: str, actual: str) -> bool:
    """Compare a case baseline, folding only documented presentation noise."""

    cmp_exp, cmp_act = expected, actual
    if case.suite == "10_optimizer":
        # Compare EXPLAIN/AP output with presentation noise folded instead of rerecording baselines.
        from .optimizer_normalize import normalize_optimizer_compare_text
        cmp_exp = normalize_optimizer_compare_text(expected)
        cmp_act = normalize_optimizer_compare_text(actual)
    elif (case.suite, case.name) == (
            "14_issue_regression", "issue_28292_ivfflat_semi_join_topk"):
        from .optimizer_normalize import normalize_cn_core_compare_text
        cmp_exp = normalize_cn_core_compare_text(expected)
        cmp_act = normalize_cn_core_compare_text(actual)
    return cmp_exp == cmp_act


def _worker_run(payload: tuple) -> dict:
    (case_dict, spec_dict, include_dirs, record, verbose, run_cfg) = payload
    case = TestCase(**{k: v if k not in ("test_file", "result_file") else Path(v)
                       for k, v in case_dict.items()})
    spec = ConnSpec(**spec_dict)
    title = _extract_title(case.test_file)
    t0 = time.time()
    interp = None

    def aborted(message):
        counts = dict(interp.counts) if interp else dict(stmts=0, ok=0, fail=1)
        counts["fail"] = max(1, counts["fail"])
        if interp and not interp.first_error:
            interp._first_error(None, message)
        return _done(case, "abort", t0, title=title, reason=message,
                     first_error=interp.first_error if interp else None,
                     integrity=interp.integrity.summary() if interp else None,
                     actual_output="\n".join(interp.output_lines) if interp else "",
                     **counts)
    try:
        if case.kind == "script":
            actual, rc = _run_script(case, spec, run_cfg)
            integrity = run_cfg.get('_integrity_summary')
            # A scenario script is graded as one unit:
            #   rc == 77 -> skip
            #   rc == 0  -> success (stmts=1, ok=1)
            #   other    -> fail    (stmts=1, ok=0, fail=1)
            if rc == 77:
                return _done(case, "skip", t0, title=title,
                             integrity=integrity,
                             reason="script exited 77 (skip)",
                             stmts=0, ok=0, fail=0)
            if rc != 0:
                return _done(case, "fail", t0, title=title,
                             integrity=integrity,
                             reason=f"script exited {rc}",
                             diff=actual[-2000:],
                             actual_output=actual,
                             failure_kind=run_cfg.get('_script_failure_kind'),
                             cleanup_required=bool(run_cfg.get('_cleanup_required')),
                             stmts=1, ok=0, fail=1)
            if record:
                case.result_file.parent.mkdir(parents=True, exist_ok=True)
                case.result_file.write_text(actual)
                return _done(case, "pass", t0, title=title,
                             integrity=integrity,
                             stmts=1, ok=1, fail=0)
            if not case.result_file.exists():
                # No baseline: success = exit 0
                return _done(case, "pass", t0, title=title,
                             integrity=integrity,
                             stmts=1, ok=1, fail=0)
            expected = case.result_file.read_text()
            if expected == actual:
                return _done(case, "pass", t0, title=title,
                             integrity=integrity,
                             stmts=1, ok=1, fail=0)
            return _done(case, "fail", t0, title=title,
                         integrity=integrity,
                         diff=_unified_diff(expected, actual, case),
                         actual_output=actual, expected_output=expected,
                         stmts=1, ok=0, fail=1)

        # kind == sql
        # SQL-mode shell directives may also invoke motr_helpers. Keep their
        # verdict isolated per case, and check it before comparing/recording.
        with tempfile.TemporaryDirectory(prefix="motr-verdict-") as verdict_dir:
            interp = Interpreter(spec, [Path(p) for p in include_dirs],
                                 Options(record=record, verbose=verbose,
                                         failure_file=Path(verdict_dir) / "failed",
                                         integrity_file=Path(verdict_dir) / "integrity.jsonl",
                                         event_sink=run_cfg.get("_event_sink"),
                                         integrity_branch=run_cfg.get('compatibility_branch'),
                                         integrity_target=run_cfg.get('integrity_target', 'matrixone'),
                                         integrity_timeout=int(run_cfg.get('integrity_timeout_sec', 20)),
                                         output_limit_bytes=int(run_cfg.get(
                                             'output_limit_bytes', 64 * 1024 * 1024))))
            actual = interp.run(case.test_file)
            interp._assert_no_reported_failures()
            counts = dict(interp.counts, first_error=interp.first_error,
                          integrity=interp.integrity.summary())
        # Continuing after an unasserted SQL error permits cleanup/diagnostics,
        # but neither a matching golden nor --record can turn it into success.
        if counts.get("fail", 0):
            return _done(case, "fail", t0, title=title,
                         reason=f"{counts['fail']} unexpected SQL error(s); "
                                "use explicit --error for negative tests",
                         actual_output=actual, **counts)
        if record:
            case.result_file.parent.mkdir(parents=True, exist_ok=True)
            case.result_file.write_text(actual)
            return _done(case, "pass", t0, title=title, **counts)
        if not case.result_file.exists():
            return _done(case, "fail", t0, title=title,
                         reason=f"missing .result file: {case.result_file}",
                         **counts)
        expected = case.result_file.read_text()
        if compare_case_output(case, expected, actual):
            return _done(case, "pass", t0, title=title, **counts)
        diff = _unified_diff(expected, actual, case)
        return _done(case, "fail", t0, title=title, diff=diff,
                     actual_output=actual, expected_output=expected, **counts)
    except TestSkip as e:
        return _done(case, "skip", t0, title=title, reason=str(e),
                     stmts=0, ok=0, fail=0)
    except OutputLimitExceeded as e:
        result = aborted(str(e))
        result.update(failure_kind='output_limit', cleanup_required=True)
        return result
    except TestAbort as e:
        return aborted(str(e))
    except Exception as e:
        tb = traceback.format_exc()
        return aborted(f"{e}\n{tb}")


def _run_script_command(
    cmd: list[str], *, env: dict, cwd: str, timeout: int,
    on_output=None, output_limit: int = 64 * 1024 * 1024,
    spool_memory: int = 1024 * 1024,
    supervised: bool = False,
):
    """Run a scenario within its supervisor's process group when available."""
    import signal
    import subprocess
    import tempfile
    import threading

    on_output = on_output or (lambda stream, text: None)
    stdout = tempfile.SpooledTemporaryFile(max_size=spool_memory, mode="w+t",
                                           encoding="utf-8", errors="replace")
    stderr = tempfile.SpooledTemporaryFile(max_size=spool_memory, mode="w+t",
                                           encoding="utf-8", errors="replace")
    output_bytes = 0
    output_lock = threading.Lock()
    output_exceeded = threading.Event()
    cleanup_required = False
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace", env=env, cwd=cwd,
        # A supervised worker already owns a private group. Do not put its
        # script in another group that the parent watchdog cannot terminate.
        start_new_session=(os.name == "posix" and not supervised),
    )

    def stop_process_group():
        try:
            if os.name == "posix" and not supervised:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def drain(pipe, spool, stream):
        nonlocal output_bytes
        try:
            # readline(size) preserves universal-newline/golden text but never
            # allocates an unbounded line before the byte quota is checked.
            chunk_size = max(1, min(4096, output_limit + 1))
            while chunk := pipe.readline(chunk_size):
                size = len(chunk.encode("utf-8"))
                with output_lock:
                    if output_bytes + size > output_limit:
                        output_exceeded.set()
                    else:
                        output_bytes += size
                        spool.write(chunk)
                if output_exceeded.is_set():
                    stop_process_group()
                    break
                on_output(stream, chunk)
        finally:
            pipe.close()

    readers = [
        threading.Thread(target=drain, args=(pipe, spool, stream), daemon=True)
        for pipe, spool, stream in ((process.stdout, stdout, "stdout"),
                                    (process.stderr, stderr, "stderr"))
    ]
    for reader in readers:
        reader.start()
    try:
        try:
            rc = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process_group()
            process.wait(timeout=5)
            rc = 124
            cleanup_required = True
            stderr.write(f"\nscript timeout ({timeout}s); verify server cleanup\n")
        for reader in readers:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            cleanup_required = True
            rc = 124
            stderr.write("\nscript descendants still hold output pipes; cleanup required\n")
        failure_kind = None
        if output_exceeded.is_set():
            cleanup_required = True
            failure_kind = "output_limit"
            rc = 125
            stderr.write(f"\nscript output exceeded {output_limit} bytes; cleanup required\n")
        stdout.seek(0)
        stderr.seek(0)
        completed = subprocess.CompletedProcess(cmd, rc, stdout.read(), stderr.read())
    except BaseException:
        # Standalone callers still own the script's private group. Under the
        # supervisor, the parent's interruption path owns the entire group.
        stop_process_group()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        stdout.close()
        stderr.close()
    completed.cleanup_required = cleanup_required
    completed.failure_kind = failure_kind
    return completed


def _run_script(case: TestCase, spec: ConnSpec, run_cfg: dict | None = None) -> tuple[str, int]:
    import subprocess
    import tempfile
    import threading
    if run_cfg is None:
        run_cfg = {}
    timeout_sec = _script_timeout_sec(case, run_cfg)
    env = os.environ.copy()
    env.update({
        "MO_HOST": spec.host,
        "MO_PORT": str(spec.port),
        "MO_USER": spec.user,
        "MO_PASSWORD": spec.password,
        "MO_DATABASE": spec.database or "",
        "PYTHONUNBUFFERED": '1',
        "MOTR_WORKSPACE": str(Path(__file__).resolve().parent.parent),
        "MOTR_SUITE": case.suite,
        "MOTR_CASE": case.name,
    })
    compatibility_branch = run_cfg.get("compatibility_branch")
    if compatibility_branch:
        env["COMPATIBILITY_BRANCH"] = str(compatibility_branch)
    else:
        env.pop("COMPATIBILITY_BRANCH", None)
    tmp = Path(tempfile.mkdtemp(prefix=f"motr-{case.suite}-{case.name}-"))
    env["MOTR_TMPDIR"] = str(tmp)
    ws = Path(__file__).resolve().parent.parent
    inc = str(ws / "include")
    env["PYTHONPATH"] = inc + (os.pathsep + env["PYTHONPATH"]
                                if env.get("PYTHONPATH") else "")
    cmd: list[str]
    abs_file = str(case.test_file.resolve())
    abs_cwd = str(case.test_file.resolve().parent)
    if case.test_file.suffix == ".sh":
        cmd = ["bash", abs_file]
    else:
        cmd = [sys.executable, abs_file]
    # Keep verdict state separate from stdout and from the scenario's scratch
    # artifacts. Its lifetime is exactly one invocation (including --repeat).
    with tempfile.TemporaryDirectory(prefix="motr-verdict-") as verdict_dir:
        failure_file = Path(verdict_dir) / "failed"
        env["MOTR_FAILURE_FILE"] = str(failure_file)
        integrity_file = Path(verdict_dir) / 'integrity.jsonl'
        env['MOTR_INTEGRITY_FILE'] = str(integrity_file)
        emit = run_cfg.get('_event_sink', lambda message: None)
        integrity = IntegrityTracker(lambda kind, **fields: emit(dict(type=kind, **fields)))
        emit(dict(type='script_start', file=abs_file, limit_seconds=timeout_sec))
        output_limit = int(run_cfg.get('output_limit_bytes', 64 * 1024 * 1024))
        spool_memory = int(run_cfg.get('output_spool_memory_bytes', 1024 * 1024))
        try:
            result = _run_script_command(
                cmd, env=env, cwd=abs_cwd, timeout=timeout_sec,
                on_output=lambda stream, line: emit(dict(
                    type='script_output', stream=stream,
                    text=safe_text(
                        line.replace(spec.password, '<redacted>')
                        if spec.password else line,
                    ),
                )),
                output_limit=output_limit, spool_memory=spool_memory,
                supervised=bool(run_cfg.get('_supervised_worker')),
            )
        except subprocess.TimeoutExpired as exc:
            run_cfg['_cleanup_required'] = True
            # Compatibility for callers/tests which provide the historical
            # TimeoutExpired contract directly.
            def as_text(value):
                return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
            return (as_text(getattr(exc, 'stdout', None) or exc.output)
                    + '\n' + as_text(exc.stderr)
                    + f'\nscript timeout ({timeout_sec}s)\n'), 124
        out, error_output, rc = result.stdout, result.stderr, result.returncode
        integrity.load_reports(integrity_file)
        if not integrity.records:
            integrity._record('not_checked', phase='script_checkpoint',
                              reason='script_requires_explicit_checkpoint')
        run_cfg['_integrity_summary'] = integrity.summary()
        emit(dict(type='integrity_summary', **run_cfg['_integrity_summary']))
        if integrity.failures and rc in (0, 77):
            out += '\nMOTR: integrity checkpoint failed\n'
            rc = 1
        if getattr(result, 'cleanup_required', False):
            run_cfg['_cleanup_required'] = True
        if getattr(result, 'failure_kind', None):
            run_cfg['_script_failure_kind'] = result.failure_kind
        if failure_file.exists() and failure_file.stat().st_size:
            out += "\nMOTR: scenario helper reported failed assertions\n"
            if rc in (0, 77):
                rc = 1
        if rc != 0 and error_output:
            out += '\n' + error_output
        return out, rc


def _done(case: TestCase, status: str, t0: float, **kw) -> dict:
    return {
        "suite": case.suite,
        "name": case.name,
        "test_file": str(case.test_file),
        "result_file": str(case.result_file),
        "status": status,
        "cost_ms": int((time.time() - t0) * 1000),
        **kw,
    }


def _unified_diff(expected: str, actual: str, case: TestCase) -> str:
    return "".join(difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=f"expected/{case.fullname}.result",
        tofile=f"actual/{case.fullname}.result",
        n=3,
    ))


# ---------- main ----------

def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _spec_from_cfg(cfg: dict, target: str = "matrixone") -> ConnSpec:
    """Pick a connection spec for the given target.

    `matrixone` (default) uses the top-level `connection:` block.
    Any other name looks up `targets.<name>` in the config.
    """
    if target in (None, "", "matrixone", "default"):
        c = cfg.get("connection", {})
    else:
        tcfg = (cfg.get("targets") or {}).get(target)
        if not tcfg:
            raise MotrError(
                f"unknown --target {target!r}; available: "
                f"matrixone, {', '.join((cfg.get('targets') or {}).keys())}"
            )
        c = tcfg
    return ConnSpec(
        host=c.get("host", "127.0.0.1"),
        port=int(c.get("port", 6001)),
        user=c.get("user", "dump"),
        password=c.get("password", "111"),
        database=c.get("database") or None,
        charset=c.get("charset", "utf8mb4"),
        connect_timeout=int(c.get("connect_timeout", 10)),
        read_timeout=int(c.get("read_timeout", 600)),
    )


def effective_parallel_workers(cli_parallel: int | None, run_cfg: dict) -> int:
    """Resolve worker count, letting an explicit CLI value override config."""
    raw = cli_parallel if cli_parallel is not None else run_cfg.get(
        "parallel_workers", 1
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def effective_compatibility_branch(
    cli_branch: str | None,
    environ: dict[str, str] | None = None,
) -> str | None:
    """Resolve one branch context, with an explicit CLI value taking priority."""
    explicit = (cli_branch or "").strip()
    if explicit:
        return explicit
    source = os.environ if environ is None else environ
    inherited = (source.get("COMPATIBILITY_BRANCH") or "").strip()
    return inherited or None


def _case_timeout_sec(payload):
    case = TestCase(**payload[0])
    cfg = payload[5]
    if case.kind == 'script':
        # Leave a short local cleanup allowance after the script's own budget.
        return _script_timeout_sec(case, cfg) + 10
    return max(1, float((cfg.get('suite_case_timeouts') or {}).get(
        case.suite, cfg.get('case_timeout_sec', 600))))


def _write_cleanup_guard(path: Path, *, run_id: str, case: str, reason: str,
                         transition: str, reports: Path) -> None:
    """Persist the first uncertainty record before terminating local work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    record = dict(schema_version=1, run_id=run_id, case=case, reason=reason,
                  transition=transition, reports=str(reports), timestamp=time.time())
    tmp = path.with_name(f'{path.name}.tmp-{os.getpid()}-{time.time_ns()}')
    try:
        with tmp.open('x', encoding='utf-8') as file:
            json.dump(record, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(path)
        if hasattr(os, 'O_DIRECTORY'):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="motr")
    ap.add_argument("tests", nargs="*", help="named tests: suite[.name] ...")
    ap.add_argument("--suite", action="append", default=[],
                    help="only these suites (repeatable)")
    ap.add_argument("--do-test", help="regex on suite.name to include")
    ap.add_argument("--skip-test", help="regex on suite.name to exclude")
    ap.add_argument("--risk-profile", choices=tuple(RISK_PROFILES),
                    help="run one complete short contract group; requires "
                    "a compatible branch and every case to pass")
    ap.add_argument("--branch", help="current branch name (e.g., main, 4.2-dev); "
                    "tests with @target-branch directive will be skipped if "
                    "they don't match this branch")
    ap.add_argument("--parallel", "-j", type=int, default=None,
                    help="number of parallel workers (overrides config)")
    ap.add_argument("--record", action="store_true",
                    help="overwrite .result with actual output")
    ap.add_argument("--target", default="matrixone",
                    help="which target to connect to "
                         "(default: matrixone; see targets: in config.yml)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--config", default=None)
    ap.add_argument("--suites-root", default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--phase", choices=['all', 'parallel', 'exclusive'], default='all',
                    help='select resource-disjoint or exclusive cases for separate CI steps')
    ap.add_argument('--acknowledge-cleanup', action='store_true',
                    help='clear the local watchdog guard after the environment was checked/reset')
    args = ap.parse_args(argv)
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    if args.risk_profile and (
        args.tests or args.suite or args.do_test is not None
        or args.skip_test is not None or args.record
        or args.parallel not in (None, 1) or args.repeat != 1
    ):
        print("--risk-profile cannot be combined with selectors, --record, "
              "or parallel/repeat values other than 1", file=sys.stderr)
        return 2

    here = Path(__file__).resolve().parent.parent
    cfg_path = Path(args.config) if args.config else here / "config.yml"
    cfg = load_config(cfg_path)
    spec = _spec_from_cfg(cfg, args.target)

    suites_root = Path(args.suites_root) if args.suites_root else \
        Path(cfg.get("run", {}).get("suites_root", here / "suites"))
    include_dirs = [here / "include", here / "std_data", suites_root]

    # Load exclude_suites from config
    exclude_suites = cfg.get("run", {}).get("exclude_suites", [])

    # Load exclusive_tests from config — these run alone (not in parallel)
    exclusive_patterns = cfg.get("run", {}).get("exclusive_tests", [])

    run_cfg = dict(cfg.get("run", {}) or {})
    run_cfg['integrity_target'] = args.target
    compatibility_branch = effective_compatibility_branch(args.branch)
    run_cfg["compatibility_branch"] = compatibility_branch

    if args.risk_profile:
        try:
            cases = resolve_risk_profile(
                suites_root, args.risk_profile, compatibility_branch,
                exclude_suites,
            )
        except MotrError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    elif args.tests:
        cases = resolve_named(suites_root, args.tests, compatibility_branch)
    else:
        cases = discover(suites_root, args.suite, args.do_test, args.skip_test,
                         exclude_suites, compatibility_branch)

    if not cases:
        print("no tests matched", file=sys.stderr)
        return 2

    parallel = (1 if args.risk_profile else
                effective_parallel_workers(args.parallel, run_cfg))
    if args.risk_profile:
        print(f"RISK PROFILE | name={args.risk_profile} | "
              f"branch={compatibility_branch} | "
              f"cases={','.join(case.fullname for case in cases)}", flush=True)

    # Split cases into parallel-safe and exclusive (must run alone).
    def _is_exclusive(case: TestCase) -> bool:
        fn = case.fullname
        return any(re.fullmatch(pat, fn) for pat in exclusive_patterns)

    exclusive_cases = [c for c in cases if _is_exclusive(c)]
    parallel_cases = [c for c in cases if not _is_exclusive(c)]
    print("RUN PLAN | "
          f"total={len(cases) * args.repeat} | repeats={args.repeat} | "
          f"parallel_workers={parallel} | "
          f"parallel_cases={len(parallel_cases)}/repeat | "
          f"exclusive_cases={len(exclusive_cases)}/repeat",
          flush=True)
    if args.phase == 'parallel':
        cases, exclusive_cases = parallel_cases, []
    elif args.phase == 'exclusive':
        cases, parallel_cases = exclusive_cases, []
    print("RUNNER | workers="
          f"{parallel} parallel_cases={len(parallel_cases)} "
          f"exclusive_cases={len(exclusive_cases)}")

    reports: list[dict] = []
    reporter = ProgressReporter(total=len(cases) * args.repeat)
    report_dir = here / cfg.get('run', {}).get('report_dir', 'var/log')
    cleanup_guard = report_dir / 'cleanup_required.json'
    if cleanup_guard.exists():
        if not args.acknowledge_cleanup:
            print(f'BLOCKED | cleanup must be verified before another run: {cleanup_guard}', flush=True)
            return 1
        cleanup_guard.rename(report_dir / f'cleanup-acknowledged-{time.time_ns()}.json')
    journal = Journal(
        report_dir / f'run-{time.time_ns()}', secrets=[spec.password],
        event_limit=int(run_cfg.get('event_journal_limit_bytes', 256 * 1024 * 1024)),
        result_limit=int(run_cfg.get('result_storage_limit_bytes', 1024 * 1024 * 1024)))
    run_cfg['_artifact_dir'] = str(journal.root / 'artifacts')
    print(f'LIVE REPORTS | {journal.root}', flush=True)
    environment_safe = True

    def mark_uncertain(detail):
        _write_cleanup_guard(
            cleanup_guard, run_id=journal.root.name,
            case=detail.get('case', 'unknown'),
            reason=detail.get('reason', 'cleanup state is uncertain'),
            transition=detail.get('transition', 'uncertain'), reports=journal.root)

    def consume(result):
        result = journal._clean(result)
        reports.append(result)
        journal.result(result)
        if result.get('cleanup_required'):
            mark_uncertain(dict(
                case=f"{result['suite']}.{result['name']}",
                reason=result.get('reason', 'case cleanup is uncertain'),
                transition=result.get('failure_kind', 'uncertain')))
        _print_row(result)
        if result['status'] in ('fail', 'abort'):
            first_error = result.get('first_error')
            detail = (json.dumps(first_error, ensure_ascii=False) if first_error
                      else safe_text(result.get('reason', 'result mismatch')))
            print('FIRST FAILURE | ' + detail, flush=True)
        sys.stdout.flush()
    t_all = time.time()
    for rep in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"=== repeat {rep}/{args.repeat} ===")
        payloads = [(
            {"suite": c.suite, "name": c.name,
             "test_file": str(c.test_file), "result_file": str(c.result_file),
             "kind": c.kind},
            spec.__dict__.copy(),
            [str(p) for p in include_dirs],
            args.record,
            args.verbose,
            run_cfg,
        ) for c in cases]

        # Separate payloads into exclusive and parallel groups.
        excl_fullnames = {c.fullname for c in exclusive_cases}
        excl_payloads = [p for p in payloads
                         if f"{p[0]['suite']}.{p[0]['name']}" in excl_fullnames]
        par_payloads = [p for p in payloads
                        if f"{p[0]['suite']}.{p[0]['name']}" not in excl_fullnames]

        for phase, selected, workers in [('parallel', par_payloads, parallel),
                                         ('exclusive', excl_payloads, 1)]:
            phase_started = time.monotonic()
            journal.phase = phase
            journal.event(dict(type='phase_start', total=len(selected), workers=workers, repeat=rep))
            def consume_phase(result, phase=phase):
                consume(result)
                reporter.advance(result, phase=phase, repeat=rep,
                                 repeat_total=args.repeat)
            if environment_safe:
                try:
                    environment_safe = execute_phase(
                        selected, workers=workers, resources=_database_resources,
                        consume=consume_phase, event=journal.event,
                        timeout_for=_case_timeout_sec,
                        heartbeat=max(.1, float(run_cfg.get('heartbeat_sec', 30))),
                        mark_uncertain=mark_uncertain)
                except BaseException as exc:
                    mark_uncertain(dict(
                        case='runner', reason=f'runner interrupted: {type(exc).__name__}: {exc}',
                        transition='parent_exception'))
                    raise
            else:
                for payload in selected:
                    consume_phase(blocked_result(payload))
            journal.event(dict(type='phase_end', total=len(selected),
                               cleanup_required=not environment_safe, repeat=rep,
                               elapsed_seconds=round(time.monotonic()-phase_started, 2)))

    total_cost = int((time.time() - t_all) * 1000)
    _print_summary(reports, total_cost)
    _write_reports(report_dir, reports)
    print('SLOWEST CASES |', flush=True)
    for result in sorted(reports, key=lambda r: r['cost_ms'], reverse=True)[:20]:
        _print_row(result)

    # Print failure details if verbose mode and there are failures
    fails = sum(1 for r in reports if r["status"] in ("fail", "abort"))
    if args.verbose and fails > 0:
        print("\n" + "=" * 70)
        print(f"FAILURE DETAILS ({fails} failed tests)")
        print("=" * 70)
        _print_failure_details(reports)
        print("\n" + "=" * 70)
        print(f"Full report saved to: {report_dir / 'failures.txt'}")
        print("=" * 70)

    if args.risk_profile:
        complete = risk_profile_passed(cases, reports)
        if not complete:
            print("RISK PROFILE NOT PASSED | every selected case must have "
                  "one terminal pass; inspect summary.txt and failures.txt",
                  file=sys.stderr)
        return 0 if complete else 1
    return 0 if fails == 0 else 1


_STATUS_TEXT = {
    "pass": "success",
    "fail": "failed",
    "abort": "failed",
    "skip": "skipped",
}


class ProgressReporter:
    """Print one unambiguous progress line for every completed test case."""

    def __init__(self, total: int, stream=None) -> None:
        self.total = total
        self.completed = 0
        self.stream = stream

    def advance(self, report: dict, *, phase: str, repeat: int,
                repeat_total: int) -> None:
        self.completed += 1
        status = _STATUS_TEXT.get(report["status"], report["status"])
        print(
            "PROGRESS | "
            f"completed={self.completed}/{self.total} | "
            f"repeat={repeat}/{repeat_total} | phase={phase} | "
            f"case={report['suite']}.{report['name']} | status={status}",
            file=self.stream,
            flush=True,
        )


def _fmt_id(r: dict) -> str:
    """Produce the [CASE-ID] token. Use the case's short name (e.g. `d-1`,
    `co-3`) uppercased so readers see familiar IDs from the testplan."""
    return f"[{r['name'].upper()}]"


def _fmt_row(r: dict) -> str:
    status = _STATUS_TEXT.get(r["status"], r["status"])
    title = (r.get("title") or "").replace("|", "/")
    if len(title) > 80:
        title = title[:77] + "..."
    stmts = r.get("stmts", 0)
    ok = r.get("ok", 0)
    fail = r.get("fail", 0)
    base = (f"{_fmt_id(r)} {title} | {status} | "
            f"stmts={stmts} ok={ok} fail={fail} "
            f"[{r['suite']} / {r['cost_ms']}ms]")
    if status != "success" and r.get("reason"):
        base += f"  -- {r['reason'][:200]}"
    return base


def _print_row(r: dict) -> None:
    print(_fmt_row(r), flush=True)


def _print_summary(reports: list[dict], total_cost_ms: int) -> None:
    by = {"pass": 0, "fail": 0, "skip": 0, "abort": 0}
    stmts = ok = fail_stmts = 0
    for r in reports:
        by[r["status"]] += 1
        stmts += r.get("stmts", 0)
        ok += r.get("ok", 0)
        fail_stmts += r.get("fail", 0)
    print("-" * 70)
    print(f"SUMMARY | total={len(reports)} success={by['pass']} "
          f"failed={by['fail'] + by['abort']} skipped={by['skip']} | "
          f"stmts={stmts} ok={ok} fail={fail_stmts} | "
          f"cost={total_cost_ms/1000:.2f}s")


def _print_failure_details(reports: list[dict]) -> None:
    """Print detailed failure information for all failed tests."""
    for r in reports:
        if r["status"] not in ("fail", "abort"):
            continue

        print("\n" + "=" * 70)
        print(f"FAILED: {r['suite']}.{r['name']}")
        print("=" * 70)
        print(f"Test file:   {r['test_file']}")
        print(f"Result file: {r['result_file']}")
        print(f"Status:      {r['status']}")
        if r.get("reason"):
            print(f"Reason:      {r['reason']}")

        # Print ALL failed SQL statements (no limit)
        if r.get("actual_output"):
            failed_sqls = []
            lines = r["actual_output"].split("\n")
            for i, line in enumerate(lines):
                if line.startswith("ERROR"):
                    if i > 0:
                        sql_line = lines[i-1].strip()
                        if sql_line and not sql_line.startswith("ERROR"):
                            failed_sqls.append((sql_line, line))

            if failed_sqls:
                print("\n" + "-" * 70)
                print("FAILED SQL STATEMENTS:")
                print("-" * 70)
                for idx, (sql, error) in enumerate(failed_sqls, 1):  # Show ALL
                    # Truncate long SQL for console output
                    sql_display = sql if len(sql) <= 80 else sql[:77] + "..."
                    error_display = error if len(error) <= 100 else error[:97] + "..."
                    print(f"\n[{idx}] SQL: {sql_display}")
                    print(f"    Error: {error_display}")

        # Print diff if available (for non-ERROR failures like output mismatch)
        if r.get("diff"):
            print("\n" + "-" * 70)
            print("OUTPUT DIFF (Expected vs Actual):")
            print("-" * 70)
            # Show first 50 lines of diff for console
            diff_lines = r["diff"].split("\n")[:50]
            print("\n".join(diff_lines))
            if len(r["diff"].split("\n")) > 50:
                print("\n... (diff truncated, see failures.txt for full output)")


def _write_reports(out_dir: Path, reports: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "summary.txt").open("w") as f:
        for r in reports:
            f.write(_fmt_row(r) + "\n")
    with (out_dir / "failures.txt").open("w") as f:
        for r in reports:
            if r["status"] in ("fail", "abort"):
                f.write("=" * 80 + "\n")
                f.write(f"FAILED: {r['suite']}.{r['name']}\n")
                f.write("=" * 80 + "\n")
                f.write(f"Test file:   {r['test_file']}\n")
                f.write(f"Result file: {r['result_file']}\n")
                f.write(f"Status:      {r['status']}\n")
                if r.get("reason"):
                    f.write(f"Reason:      {r['reason']}\n")
                f.write("\n")

                # Add test SQL content for failed cases
                try:
                    test_path = Path(r['test_file'])
                    if test_path.exists() and test_path.suffix == ".test":
                        f.write("-" * 80 + "\n")
                        f.write("TEST SQL:\n")
                        f.write("-" * 80 + "\n")
                        f.write(test_path.read_text())
                        f.write("\n")
                except Exception:
                    pass

                # Extract and highlight failed SQL statements from actual output
                if r.get("actual_output"):
                    failed_sqls = []
                    lines = r["actual_output"].split("\n")
                    for i, line in enumerate(lines):
                        if line.startswith("ERROR"):
                            # The SQL is typically on the previous line
                            if i > 0:
                                sql_line = lines[i-1].strip()
                                if sql_line and not sql_line.startswith("ERROR"):
                                    failed_sqls.append((sql_line, line))

                    if failed_sqls:
                        f.write("-" * 80 + "\n")
                        f.write("FAILED SQL STATEMENTS:\n")
                        f.write("-" * 80 + "\n")
                        for idx, (sql, error) in enumerate(failed_sqls, 1):
                            f.write(f"\n[{idx}] SQL: {sql}\n")
                            f.write(f"    Error: {error}\n")
                        f.write("\n")

                # Add detailed diff with actual vs expected
                if r.get("diff"):
                    f.write("-" * 80 + "\n")
                    f.write("DIFF (Expected vs Actual):\n")
                    f.write("-" * 80 + "\n")
                    f.write(r["diff"])
                    f.write("\n")

                # Write full expected output
                if r.get("expected_output"):
                    f.write("-" * 80 + "\n")
                    f.write("EXPECTED OUTPUT:\n")
                    f.write("-" * 80 + "\n")
                    f.write(r["expected_output"])
                    if not r["expected_output"].endswith("\n"):
                        f.write("\n")
                    f.write("\n")

                # Write full actual output
                if r.get("actual_output"):
                    f.write("-" * 80 + "\n")
                    f.write("ACTUAL OUTPUT:\n")
                    f.write("-" * 80 + "\n")
                    f.write(r["actual_output"])
                    if not r["actual_output"].endswith("\n"):
                        f.write("\n")
                    f.write("\n")

                f.write("\n")


def cli(argv: list[str] | None = None) -> int:
    """Make a soft CLI stop unwind the existing supervisor cleanup path.

    Worker processes own separate process groups. Exiting immediately on TERM
    would orphan those groups and bypass the durable cleanup guard. Keep signal
    ownership at the CLI boundary so importing/calling main does not replace an
    embedding application's handlers.
    """
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        # A repeated TERM must not interrupt guard persistence or group cleanup
        # while SystemExit unwinds execute_phase's except/finally blocks.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, terminate)
        return main(argv)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(cli())
