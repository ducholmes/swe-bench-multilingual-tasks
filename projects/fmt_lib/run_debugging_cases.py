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
from pathlib import Path

from runners import CppRunner

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
OUT = ROOT / "debugging" / "out"
FRAMEWORK = (
    Path(__file__).resolve().parents[2]
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


def cpp_target_for_task(task: Path, failing_test: str) -> str:
    """Find the CMake test target used by a C++ SWE-bench task.

    The patched test file is authoritative.  For example, fmt-3272 reports
    ``locale_test.localized_double`` but patches ``test/xchar-test.cc``.
    """
    patch = task / "test.patch"
    if patch.is_file():
        files = re.findall(r"^\+\+\+ b/test/([^\s]+)$", patch.read_text(), re.MULTILINE)
        targets = {
            Path(filename).stem
            for filename in files
            if Path(filename).name.endswith("-test.cc")
        }
        if len(targets) == 1:
            return targets.pop()
    return CppRunner.target_for_test(failing_test)


def prepare(task_id: str, image: str | None = None) -> Path:
    task, metadata, tests = load(task_id)
    image = image or metadata.get("image")
    if not image:
        raise ValueError(f"No image in {task}/task.yaml; pass --image")
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
    clean = run(["git", "-C", str(project), "clean", "-fdX"])
    if clean.returncode:
        raise RuntimeError("could not clean extracted project")
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

    # A target command is also rendered once with the literal ``{test_id}``
    # placeholder for Debugging-Framework.  Resolve the CMake target from the
    # actual failing test first so that the placeholder keeps the right binary.
    failing = [str(value).strip() for value in tests.get("FAIL_TO_PASS", [])]
    if not failing:
        raise ValueError(f"{task_id} has no FAIL_TO_PASS tests")
    test_target = cpp_target_for_task(task, failing[0])
    runner = CppRunner(test_target=test_target)
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
    cleanup = run([
        "docker", "run", "--rm", "-v", f"{project}:/testbed", "-w", "/testbed",
        image, "git", "-c", "safe.directory=/testbed", "clean", "-fdX",
    ])
    if cleanup.returncode:
        raise RuntimeError("could not clean build artifacts after baseline test")
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
        "regression_test": [runner.regression_command()],
        "repair": {"failing_tests": failing},
        "environment": {"mode": "image", "runtime": "docker", "image": image},
        "metadata": {
            "base_commit": metadata.get("base_commit"),
            "repo": metadata.get("repo"),
            "test_target": test_target,
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
) -> int:
    path = prepare(task_id, image)
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
            status = run_instance(args.action, instance, args.image)
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
