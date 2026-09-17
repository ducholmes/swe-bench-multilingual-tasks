#!/usr/bin/env python3
"""Materialize Redis SWE-bench tasks as Debugging-Framework inputs."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
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
    runner = RedisRunner(redis_test_command(task))
    command = runner.target_command(failing[0])
    failure = target / "failure.log"
    failure.write_text(f"INSTANCE: {task_id}\nCOMMAND: {shlex.join(command)}\n\n")
    result = run(["docker", "run", "--rm", "-v", f"{project}:/testbed", "-w", "/testbed", image, *command])
    failure.write_text(failure.read_text() + (result.stdout or "") + f"\nEXIT_CODE: {result.returncode}\n")
    clean_project(project, image)
    config = {"schema_version": 6, "project_id": task_id, "language": runner.language, "system": "make", "setup": [], "build": [runner.build_command()], "target_test": [{"command": runner.target_command("{test_id}"), "evidence_pattern": r"^(?:PASSED|FAILED)\s+\S+", "failure_pattern": r"^FAILED\s+\S+"}], "regression_test": [{"command": runner.regression_command(), "evidence_pattern": r"\\o/ All tests passed without errors!", "failure_pattern": r"!!! WARNING|FAILED"}], "repair": {"failing_tests": failing}, "environment": {"mode": "image", "runtime": "docker", "image": image}, "metadata": {"base_commit": metadata.get("base_commit"), "repo": metadata.get("repo"), "test_command": runner.test_command, "original_failing_tests": original_failing}}
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
