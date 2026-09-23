#!/usr/bin/env python3
"""Materialize Redis SWE-bench tasks as Debugging-Framework inputs."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from runners import RedisRunner

# run_debugging_cases.py lives in <repo>/projects/redis/.  The task and
# debugging directories live at <repo>/tasks and <repo>/debugging.
ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tasks"
# Keep Redis inputs in their own namespace so they do not mix with inputs from
# other repositories under debugging/out.
OUT = ROOT / "debugging" / "out" / "redis"
FRAMEWORK = ROOT.parent / "Debugging-Framework" / ".venv" / "bin" / "debugging-framework"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    print("+", shlex.join(cmd), flush=True)
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    return result


def load(task_id: str) -> tuple[Path, dict, dict]:
    task = TASKS / task_id
    metadata = {}
    for line in (task / "task.yaml").read_text().splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("'\"")
    return task, metadata, json.loads((task / "tests.json").read_text())


def redis_test_command(task: Path) -> str:
    lines = [line.strip() for line in (task / "eval.sh").read_text().splitlines() if "./runtest --durable --single" in line]
    if not lines:
        raise ValueError(f"No Redis runtest command found in {task}/eval.sh")
    command = re.sub(r"^\(?(.*?)\)?\s*\|\s*cat$", r"\1", lines[-1])
    return command.replace("TERM=dumb ", "")


def normalize_test_id(test_id: str) -> str:
    """Use one token because Debugging-Framework parses only the first word."""
    return re.sub(r"\s+", "_", str(test_id).strip())


def build_image(task: Path, image: str) -> None:
    dockerfile = task / "Dockerfile"
    if not dockerfile.is_file():
        raise ValueError(f"No Dockerfile found at {dockerfile}")
    result = run([
        "docker", "build", "-t", image,
        "-f", str(dockerfile), str(task),
    ])
    if result.returncode:
        raise RuntimeError(f"could not build Docker image {image}")


def clean_project(project: Path, image: str) -> None:
    """Remove container-created build/test files with Docker root privileges."""
    result = run([
        "docker", "run", "--rm", "-v", f"{project}:/testbed",
        "-w", "/testbed", image, "git", "-c", "safe.directory=/testbed",
        "clean", "-fdX",
    ])
    if result.returncode:
        raise RuntimeError("could not clean extracted project")


def run_in_image(project: Path, image: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return run([
        "docker", "run", "--rm", "-v", f"{project}:/testbed",
        "-w", "/testbed", image, *command,
    ])


def test_failure_details(result: subprocess.CompletedProcess[str]) -> str:
    """Extract useful Redis/Tcl or build diagnostics from a failed command."""
    lines = (result.stdout or "").splitlines()
    matches = [
        index for index, line in enumerate(lines)
        if re.search(
            r"\[err\]:|\[exception\]|!!! WARNING|\bFAILED\b|\bfailed\b|"
            r"(?:fatal )?error:|Assertion `",
            line,
            re.IGNORECASE,
        )
    ]
    # Redis often prints the test name in an [err]: line and its assertion or
    # compiler diagnostic immediately after it.  Keep enough context to make
    # the raised error actionable without duplicating an entire test log.
    if matches:
        selected = set()
        for index in matches:
            selected.update(range(max(0, index - 1), min(len(lines), index + 3)))
        excerpt = [lines[index] for index in sorted(selected)][-30:]
    else:
        excerpt = lines[-30:]
    return "\n".join(excerpt).strip() or "no diagnostic output captured"


def oracle_failed_tests(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Return normalized IDs emitted by the exact per-test oracle command."""
    return list(dict.fromkeys(re.findall(
        r"^ORACLE_FAILED\s+(\S+)$", result.stdout or "", re.MULTILINE
    )))


class FixedTargetFailure(RuntimeError):
    """The gold state does not pass at least one declared target test."""


def verify_oracle(
    *, task: Path, project: Path, image: str, runner: RedisRunner,
    failing_tests: list[str], passing_tests: list[str],
) -> list[str]:
    """Validate declared tests exactly and find fixed-state regressions."""
    failing_ids = [normalize_test_id(test) for test in failing_tests]
    passing_ids = [normalize_test_id(test) for test in passing_tests]

    # A single broad eval.sh command only proves that some test failed.  Run
    # every FAIL_TO_PASS ID with an exact --only selector instead.
    buggy_result = run_in_image(
        project, image, runner.oracle_command(failing_ids)
    )
    buggy_failed = set(oracle_failed_tests(buggy_result))
    buggy_passed = [
        test for test, test_id in zip(failing_tests, failing_ids)
        if test_id not in buggy_failed
    ]
    if buggy_passed:
        raise RuntimeError(
            "buggy state unexpectedly passes FAIL_TO_PASS test(s): "
            + ", ".join(buggy_passed)
        )
    if buggy_result.returncode and len(buggy_failed) != len(failing_ids):
        missing = [test for test_id, test in zip(failing_ids, failing_tests) if test_id not in buggy_failed]
        raise RuntimeError(
            "buggy exact oracle did not produce a result for test(s): "
            + ", ".join(missing) + "\n" + test_failure_details(buggy_result)
        )
    if not buggy_failed:
        raise RuntimeError("buggy state unexpectedly passes all FAIL_TO_PASS tests")
    clean_project(project, image)

    gold_patch = task / "gold.patch"
    if not gold_patch.is_file():
        raise ValueError(f"No gold.patch found at {gold_patch}")
    fixed_parent = Path(tempfile.mkdtemp(prefix="df-fixed-oracle-", dir=project.parent))
    fixed_project = fixed_parent / project.name
    try:
        shutil.copytree(project, fixed_project)
        # test.patch is already committed in ``project``.  gold.patch may
        # repeat its test hunks, so apply only the fixed production changes.
        check = run([
            "git", "-C", str(fixed_project), "apply", "--check",
            "--exclude=tests/**", str(gold_patch.resolve()),
        ])
        if check.returncode:
            raise RuntimeError("could not apply gold.patch source hunks for fixed oracle")
        applied = run([
            "git", "-C", str(fixed_project), "apply", "--exclude=tests/**",
            str(gold_patch.resolve()),
        ])
        if applied.returncode:
            raise RuntimeError("could not apply gold.patch source hunks for fixed oracle")
        declared_ids = [*failing_ids, *passing_ids]
        fixed_result = run_in_image(
            fixed_project, image, runner.oracle_command(declared_ids)
        )
        fixed_failed_ids = set(oracle_failed_tests(fixed_result))
        target_failures = [
            test for test, test_id in zip(failing_tests, failing_ids)
            if test_id in fixed_failed_ids
        ]
        if target_failures:
            raise FixedTargetFailure(
                "fixed state fails FAIL_TO_PASS test(s): "
                + ", ".join(target_failures) + "\n"
                + test_failure_details(fixed_result)
            )
        if fixed_result.returncode and len(fixed_failed_ids) == 0:
            raise RuntimeError(
                "fixed exact oracle failed without per-test markers:\n"
                + test_failure_details(fixed_result)
            )

        # PASS_TO_PASS failures on the gold state are excluded from regression.
        excluded = [
            test for test, test_id in zip(passing_tests, passing_ids)
            if test_id in fixed_failed_ids
        ]
        return excluded
    finally:
        # Docker's build can create root-owned ignored files; clean from the
        # container before removing the disposable fixed checkout on the host.
        if fixed_project.exists():
            try:
                clean_project(fixed_project, image)
            except (OSError, RuntimeError) as exc:
                print(f"[warning] could not clean fixed oracle checkout: {exc}", flush=True)
        shutil.rmtree(fixed_parent, ignore_errors=True)


def prepare(task_id: str, image: str | None = None, build: bool = False) -> Path:
    task, metadata, tests = load(task_id)
    image = image or metadata.get("image")
    if not image:
        raise ValueError(f"No image in {task}/task.yaml; pass --image")
    if build:
        build_image(task, image)
    original_failing = [str(x).strip() for x in tests.get("FAIL_TO_PASS", [])]
    if not original_failing:
        raise ValueError(f"{task_id} has no FAIL_TO_PASS tests")
    original_passing = [str(x).strip() for x in tests.get("PASS_TO_PASS", [])]
    if not original_passing:
        raise ValueError(f"{task_id} has no PASS_TO_PASS tests")
    if len(set(original_failing)) != len(original_failing) or len(set(original_passing)) != len(original_passing):
        raise ValueError(f"{task_id} contains duplicate declared test IDs")
    overlap = set(original_failing) & set(original_passing)
    if overlap:
        raise ValueError(
            f"{task_id} declares test(s) in both FAIL_TO_PASS and PASS_TO_PASS: "
            + ", ".join(sorted(overlap))
        )
    failing = [normalize_test_id(x) for x in original_failing]
    target = OUT / task_id
    project = target / task_id
    target.mkdir(parents=True, exist_ok=True)
    if project.exists() and not (project / ".git").exists():
        shutil.rmtree(project)
    if not project.exists():
        project.mkdir()
        container = f"df-prepare-{task_id.replace('_', '-')}"
        created = run(["docker", "create", "--name", container, image])
        if created.returncode:
            raise RuntimeError(created.stdout)
        try:
            copied = run(["docker", "cp", f"{container}:/testbed/.", str(project)])
            if copied.returncode:
                raise RuntimeError(copied.stdout)
        finally:
            run(["docker", "rm", container])
    if not (project / ".git").exists():
        raise RuntimeError(f"No Git checkout extracted at {project}")
    clean_project(project, image)
    patch = task / "test.patch"
    if patch.is_file() and run(["git", "-C", str(project), "apply", "--check", str(patch.resolve())]).returncode == 0:
        if run(["git", "-C", str(project), "apply", str(patch.resolve())]).returncode:
            raise RuntimeError("could not apply test.patch")
        run(["git", "-C", str(project), "config", "user.email", "debugging-framework@local"])
        run(["git", "-C", str(project), "config", "user.name", "Debugging Framework"])
        run(["git", "-C", str(project), "add", "-A"])
        run(["git", "-C", str(project), "commit", "-m", "SWE-bench test baseline"])
    target_names: dict[str, str] = {}
    for original in [*original_failing, *original_passing]:
        normalized = normalize_test_id(original)
        previous = target_names.setdefault(normalized, original)
        if previous != original:
            raise ValueError(
                f"{task_id} has ambiguous normalized target ID {normalized!r}: "
                f"{previous!r} and {original!r}"
            )
    runner = RedisRunner(
        redis_test_command(task), tuple(target_names.items())
    )
    try:
        excluded_regressions = verify_oracle(
            task=task,
            project=project,
            image=image,
            runner=runner,
            failing_tests=original_failing,
            passing_tests=original_passing,
        )
    except FixedTargetFailure as exc:
        # This instance cannot be evaluated: its gold state does not satisfy
        # a required target outcome.  Do not leave a stale input behind.
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"{exc}\nRejected instance and removed {target}") from exc
    if excluded_regressions:
        print(
            f"[filter] {task_id}: skip PASS_TO_PASS test(s) that fail on fixed: "
            + ", ".join(excluded_regressions),
            flush=True,
        )
    command = runner.target_command(failing[0])
    failure = target / "failure.log"
    failure.write_text(f"INSTANCE: {task_id}\nCOMMAND: {shlex.join(command)}\n\n")
    result = run(["docker", "run", "--rm", "-v", f"{project}:/testbed", "-w", "/testbed", image, *command])
    failure.write_text(failure.read_text() + (result.stdout or "") + f"\nEXIT_CODE: {result.returncode}\n")
    clean_project(project, image)
    config = {
        "schema_version": 6,
        "project_id": task_id,
        "language": runner.language,
        "system": "make",
        "setup": [],
        "build": [runner.build_command()],
        "target_test": [{
            "command": runner.target_command("{test_id}"),
            "evidence_pattern": r"^(?:PASSED|FAILED)\s+\S+",
            "failure_pattern": r"^FAILED\s+\S+",
        }],
        "regression_test": [{
            "command": runner.regression_command(original_passing, excluded_regressions),
            "evidence_pattern": r"^REGRESSION_PASSED\s+\S+",
            "failure_pattern": r"^REGRESSION_(?:FAILED|INVALID)\s+\S+",
        }],
        "repair": {"failing_tests": failing},
        "environment": {"mode": "image", "runtime": "docker", "image": image},
        "metadata": {
            "base_commit": metadata.get("base_commit"),
            "repo": metadata.get("repo"),
            "test_command": runner.test_command,
            "original_failing_tests": original_failing,
            "regression_tests": [
                test for test in original_passing if test not in excluded_regressions
            ],
            "fixed_failed_regression_tests": excluded_regressions,
        },
    }
    (target / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    return target


def discover_instances() -> list[str]:
    return sorted(p.name for p in TASKS.iterdir() if p.is_dir() and p.name.startswith("redis__redis-") and (p / "task.yaml").is_file())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "doctor", "repair", "trial"])
    parser.add_argument("--instance-id")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--image")
    parser.add_argument(
        "--build",
        action="store_true",
        help="build the task Docker image before preparing the input",
    )
    args = parser.parse_args()
    if args.all == bool(args.instance_id):
        raise SystemExit("provide exactly one of --all or --instance-id")
    failures = []
    for task_id in discover_instances() if args.all else [args.instance_id]:
        try:
            task_path = OUT / task_id
            if args.action == "prepare":
                path = prepare(task_id, args.image, args.build)
            else:
                path = task_path
                if not (path / "config.json").is_file() or not (path / "failure.log").is_file():
                    raise RuntimeError(
                        f"input not found at {path}; run prepare first"
                    )
            project, config = path / task_id, path / "config.json"
            if args.action in ("doctor", "trial") and run([str(FRAMEWORK), "doctor", str(project), "--config", str(config)]).returncode:
                raise RuntimeError("doctor failed")
            if args.action in ("repair", "trial") and run([str(FRAMEWORK), "repair", "--project", str(project), "--config", str(config), "--failure-output", str(path / "failure.log")]).returncode:
                raise RuntimeError("repair failed")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"[error] {task_id}: {exc}")
            failures.append(task_id)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
