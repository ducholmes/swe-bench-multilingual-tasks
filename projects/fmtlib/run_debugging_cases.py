#!/usr/bin/env python3
"""Prepare SWE-bench instances for Debugging-Framework.

It creates the framework's project/config/failure.log input contract for one
instance or a deterministic batch of instances.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from runners import CppRunner

# The task and debugging directories are siblings of ``projects``.
ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tasks"
# Keep fmtlib inputs grouped separately from other SWE-bench projects.
OUT = ROOT / "debugging" / "out" / "fmtlib"
FRAMEWORK = (
    Path(__file__).resolve().parents[2]
    .parent
    / "Debugging-Framework"
    / ".venv"
    / "bin"
    / "debugging-framework"
)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    print("+", shlex.join(cmd), flush=True)
    result = subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs
    )
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.returncode:
        print(f"[exit {result.returncode}]", flush=True)
    return result


def load(task_id: str) -> tuple[Path, dict, dict]:
    task = TASKS / task_id
    metadata = {}
    for line in (task / "task.yaml").read_text().splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("'\"")
    tests = json.loads((task / "tests.json").read_text())
    return task, metadata, tests


def build_image(task: Path, image: str) -> None:
    """Build a task's SWE-bench image using its task-local Dockerfile."""
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
    """Clean container-owned build outputs through Docker's user."""
    result = run([
        "docker", "run", "--rm", "-v", f"{project}:/testbed",
        "-w", "/testbed", image, "git", "-c", "safe.directory=/testbed",
        "clean", "-fdX",
    ])
    if result.returncode:
        raise RuntimeError("could not clean extracted project")


def run_in_image(project: Path, image: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a test command in the task image against ``project``."""
    return run([
        "docker", "run", "--rm", "-v", f"{project}:/testbed",
        "-w", "/testbed", image, *command,
    ])


class FixedTargetFailure(RuntimeError):
    """The gold state does not satisfy a declared FAIL_TO_PASS test."""


def oracle_failed_tests(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Read the per-ID markers emitted by CppRunner.target_group_command."""
    return list(dict.fromkeys(re.findall(
        r"^ORACLE_FAILED\s+(\S+)$", result.stdout or "", re.MULTILINE
    )))


def verify_oracle(
    *, task: Path, project: Path, image: str, failing_tests: list[str], passing_tests: list[str],
) -> list[str]:
    """Prove that the task's declared test oracle is internally consistent.

    The checkout contains ``test.patch``.  ``gold.patch`` normally repeats
    those test changes, so only its source-file hunks are applied to a
    disposable copy when building the fixed state.  A task is accepted only
    when every FAIL_TO_PASS test fails on buggy and every declared test passes
    after the gold source change.  Without the latter check an unavailable or
    malformed test can make repair evaluation look better than it is.
    """
    def command_for(test_id: str) -> list[str]:
        return CppRunner().target_command(test_id)

    def passing_groups() -> list[tuple[list[str], list[str]]]:
        """Run all declared IDs after resolving each to its real executable."""
        ids = [*failing_tests, *passing_tests]
        return [(ids, CppRunner().target_group_command(ids))]

    # Check each failing ID separately: a non-zero status for a group would
    # not prove that every member of FAIL_TO_PASS actually fails.
    buggy_passed = [
        test_id for test_id in failing_tests
        if run_in_image(project, image, command_for(test_id)).returncode == 0
    ]
    if buggy_passed:
        raise RuntimeError(
            "buggy state unexpectedly passes FAIL_TO_PASS test(s): "
            + ", ".join(buggy_passed)
        )
    clean_project(project, image)

    gold_patch = task / "gold.patch"
    if not gold_patch.is_file():
        raise ValueError(f"No gold.patch found at {gold_patch}")
    fixed_parent = Path(tempfile.mkdtemp(prefix="df-fixed-oracle-", dir=project.parent))
    fixed_project = fixed_parent / project.name
    try:
        shutil.copytree(project, fixed_project)
        # gold.patch includes the same test hunks as test.patch.  They are
        # already present in the prepared baseline; applying just production
        # hunks models the evaluator's fixed source plus its test patch.
        checked = run([
            "git", "-C", str(fixed_project), "apply", "--check",
            "--exclude=test/**", str(gold_patch.resolve()),
        ])
        if checked.returncode:
            raise RuntimeError("could not apply gold.patch source hunks for fixed oracle")
        applied = run([
            "git", "-C", str(fixed_project), "apply", "--exclude=test/**",
            str(gold_patch.resolve()),
        ])
        if applied.returncode:
            raise RuntimeError("could not apply gold.patch source hunks for fixed oracle")

        for test_ids, command in passing_groups():
            fixed_result = run_in_image(fixed_project, image, command)
            if fixed_result.returncode == 0:
                continue
            failed_tests = oracle_failed_tests(fixed_result)
            if not failed_tests:
                raise RuntimeError(
                    "fixed state test command failed without per-test oracle markers"
                )
            target_failures = [test for test in failing_tests if test in failed_tests]
            if target_failures:
                raise FixedTargetFailure(
                    "fixed state fails FAIL_TO_PASS test(s): "
                    + ", ".join(target_failures)
                )
            return [test for test in passing_tests if test in failed_tests]
        return []
    finally:
        # Test execution in Docker may leave root-owned ignored build files.
        # Remove them as the container user before removing the disposable
        # fixed checkout from the host; otherwise shutil.rmtree can silently
        # leave ``df-fixed-oracle-*`` directories behind.
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
    target = OUT / task_id
    # Debugging-Framework derives the experiment directory from the supplied
    # project's basename.  Keep the instance ID here instead of a generic
    # ``project`` directory, otherwise every run is stored as experiments/project.
    project = target / task_id
    target.mkdir(parents=True, exist_ok=True)
    # An earlier failed extraction could leave an empty directory.  Never let
    # git commands discover and mutate the parent APR repository in that case.
    if project.exists() and not (project / ".git").exists():
        shutil.rmtree(project)
    if not project.exists():
        project.mkdir()
        container = f"df-prepare-{task_id.replace('_', '-') }"
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
        raise RuntimeError(f"container extraction did not produce a Git checkout: {project}")

    # The SWE-bench image contains the prebuilt repository.  Remove ignored
    # build outputs before handing it to Debugging-Framework, then apply the
    # test patch exactly as the SWE-bench evaluator does.
    clean_project(project, image)
    test_patch = task / "test.patch"
    if test_patch.is_file():
        check = run(["git", "-C", str(project), "apply", "--check", str(test_patch.resolve())])
        if check.returncode == 0:
            applied = run(["git", "-C", str(project), "apply", str(test_patch.resolve())])
            if applied.returncode:
                raise RuntimeError("could not apply test.patch")
            # The framework requires a clean Git checkout.  The SWE-bench
            # evaluator applies test.patch before testing; record that same
            # baseline as a local commit so it remains available to the test
            # command without appearing as a repair change.
            run(["git", "-C", str(project), "config", "user.email", "debugging-framework@local"])
            run(["git", "-C", str(project), "config", "user.name", "Debugging Framework"])
            committed = run(["git", "-C", str(project), "add", "-A"])
            if committed.returncode:
                raise RuntimeError("could not stage test.patch baseline")
            committed = run(["git", "-C", str(project), "commit", "-m", "SWE-bench test baseline"])
            if committed.returncode:
                raise RuntimeError("could not commit test.patch baseline")

    # The command dynamically discovers the executable that owns a GoogleTest
    # ID, so it remains valid when a fmt version combines suites in one binary.
    failing = [str(value).strip() for value in tests.get("FAIL_TO_PASS", [])]
    if not failing:
        raise ValueError(f"{task_id} has no FAIL_TO_PASS tests")
    passing = [str(value).strip() for value in tests.get("PASS_TO_PASS", [])]
    if not passing:
        raise ValueError(f"{task_id} has no PASS_TO_PASS tests")
    if len(set(failing)) != len(failing) or len(set(passing)) != len(passing):
        raise ValueError(f"{task_id} contains duplicate declared test IDs")
    overlap = set(failing) & set(passing)
    if overlap:
        raise ValueError(
            f"{task_id} declares test(s) in both FAIL_TO_PASS and PASS_TO_PASS: "
            + ", ".join(sorted(overlap))
        )

    try:
        excluded_regressions = verify_oracle(
            task=task,
            project=project,
            image=image,
            failing_tests=failing,
            passing_tests=passing,
        )
    except FixedTargetFailure as exc:
        # No valid repair input exists when the gold state itself fails a
        # required target; remove every artifact for this rejected instance.
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"{exc}\nRejected instance and removed {target}") from exc
    if excluded_regressions:
        print(
            f"[filter] {task_id}: skip regression test(s) that fail on fixed: "
            + ", ".join(excluded_regressions),
            flush=True,
        )
    runner = CppRunner()
    # Keep a concrete command for producing the caller-supplied failure log,
    # and a placeholder command for the framework.  The latter is expanded
    # once per repair.failing_tests entry during target validation.
    command = runner.target_command(failing[0])
    target_template = runner.target_command("{test_id}")
    failure = target / "failure.log"
    failure.write_text(
        "INSTANCE: " + task_id + "\nCOMMAND: " + shlex.join(command) + "\n\n"
    )
    # Execute inside the prepared image; the output is the caller-supplied baseline.
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{project}:/testbed",
            "-w",
            "/testbed",
            image,
            *command,
        ]
    )
    with failure.open("a") as fh:
        fh.write(result.stdout or "")
        fh.write(f"\nEXIT_CODE: {result.returncode}\n")
    # Running the baseline test recreates ignored Maven/Cargo build outputs.
    # Remove them after capturing the log, otherwise doctor quite correctly
    # rejects the checkout as unsafe for in-place recovery.
    clean_project(project, image)
    config = {
        "schema_version": 6,
        "project_id": task_id,
        "language": "cpp",
        "setup": [],
        # Debugging-Framework expects a list of argv commands, not one flat
        # argv list.  A flat list is interpreted as many one-word commands.
        "build": [runner.build_command()] if runner.build_command() else [],
        "target_test": [{
            "command": target_template,
            "evidence_pattern": r"^(?:PASSED|FAILED)\s+\S+",
            "failure_pattern": r"^FAILED\s+\S+",
        }],
        "regression_test": [runner.regression_command(passing, excluded_regressions)],
        "repair": {"failing_tests": failing},
        "environment": {"mode": "image", "runtime": "docker", "image": image},
        "metadata": {
            "base_commit": metadata.get("base_commit"),
            "repo": metadata.get("repo"),
            "test_target": "auto-discovered",
            "fixed_failed_regression_tests": excluded_regressions,
        },
    }
    config["system"] = "cmake"
    (target / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    print(target)
    return target


def discover_instances() -> list[str]:
    """Return all fmtlib/fmt task IDs in deterministic order."""
    instances = []
    for task in sorted(TASKS.iterdir()):
        if not task.is_dir() or not (task / "task.yaml").is_file():
            continue
        _, metadata, _ = load(task.name)
        if metadata.get("repo") == "fmtlib/fmt" and task.name.startswith("fmtlib__fmt-"):
            instances.append(task.name)
    return instances


def run_instance(
    action: str,
    task_id: str,
    image: str | None = None,
    build: bool = False,
) -> int:
    path = prepare(task_id, image, build)
    if action == "prepare":
        return 0
    framework = str(FRAMEWORK)
    config, failure = path / "config.json", path / "failure.log"
    project = path / task_id
    doctor = [framework, "doctor", str(project), "--config", str(config)]
    if action in ("doctor", "trial"):
        if run(doctor).returncode:
            return 1
    if action in ("repair", "trial"):
        return run(
            [
                framework,
                "repair",
                "--project",
                str(project),
                "--config",
                str(config),
                "--failure-output",
                str(failure),
            ]
        ).returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "doctor", "repair", "trial"])
    parser.add_argument("--instance-id")
    parser.add_argument(
        "--all",
        action="store_true",
        help="run the action for every fmtlib/fmt task",
    )
    parser.add_argument("--image")
    parser.add_argument(
        "--build",
        action="store_true",
        help="build the task Docker image before preparing the input",
    )
    args = parser.parse_args()
    if args.all and args.instance_id:
        raise SystemExit("--all and --instance-id cannot be used together")

    if args.all:
        instances = discover_instances()
        if not instances:
            raise SystemExit("No fmtlib/fmt tasks found")
    else:
        instance = args.instance_id
        if not instance:
            raise SystemExit("--instance-id is required unless --all is used")
        instances = [instance]

    failures = []
    for instance in instances:
        print(f"\n=== {instance} (cpp/fmtlib) ===", flush=True)
        try:
            status = run_instance(args.action, instance, args.image, args.build)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"[error] {instance}: {exc}", flush=True)
            status = 1
        if status:
            failures.append(instance)
            if not args.all:
                return status

    if failures:
        print("\nFailed instances: " + ", ".join(failures), flush=True)
        return 1
    print(f"\nCompleted {len(instances)} instance(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
