#!/usr/bin/env python3
"""Prepare SWE-bench instances for Debugging-Framework.

This first version deliberately handles three small smoke-test instances:
Gson (Java), MicroPython tests (Python-test fallback), and uutils (Rust).
It creates the framework's project/config/failure.log input contract.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from runners import runner_for

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
OUT = ROOT / "debugging" / "out"
DEFAULTS = {
    "java": "google__gson-2479",
    "python": "micropython__micropython-10095",
    "rust": "uutils__coreutils-6377",
}
FRAMEWORK = (
    Path(__file__).resolve().parents[2]
    / "Debugging-Framework"
    / ".venv"
    / "bin"
    / "debugging-framework"
)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
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


def prepare(task_id: str, language: str, image: str | None = None) -> Path:
    task, metadata, tests = load(task_id)
    image = image or metadata.get("image")
    if not image:
        raise ValueError(f"No image in {task}/task.yaml; pass --image")
    target = OUT / task_id
    project = target / "project"
    target.mkdir(parents=True, exist_ok=True)
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

    micropython = task_id.startswith("micropython__")
    module = "gson" if task_id.startswith("google__gson-") else None
    runner = runner_for(language, micropython=micropython, module=module)
    failing = tests.get("FAIL_TO_PASS", [])
    if not failing:
        raise ValueError(f"{task_id} has no FAIL_TO_PASS tests")
    command = runner.target_command(failing[0])
    failure = target / "failure.log"
    failure.write_text("INSTANCE: " + task_id + "\nCOMMAND: " + " ".join(command) + "\n\n")
    # Execute inside the prepared image; the output is the caller-supplied baseline.
    result = run(["docker", "run", "--rm", "-v", f"{project}:/testbed", "-w", "/testbed", image, *command])
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
        "language": language,
        "setup": [],
        "build": runner.build_command(),
        "target_test": command,
        "regression_test": runner.regression_command(),
        "repair": {"failing_tests": failing},
        "environment": {"mode": "image", "runtime": "docker", "image": image},
        "metadata": {"base_commit": metadata.get("base_commit"), "repo": metadata.get("repo")},
    }
    (target / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    print(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "doctor", "repair", "trial"])
    parser.add_argument("--language", choices=["c", "cpp", "java", "python", "rust"])
    parser.add_argument("--instance-id")
    parser.add_argument("--image")
    args = parser.parse_args()
    language = args.language or ("python" if args.instance_id and args.instance_id.startswith("micropython__") else None)
    if not language:
        raise SystemExit("--language is required unless --instance-id is micropython__...")
    instance = args.instance_id or DEFAULTS[language]
    path = prepare(instance, language, args.image)
    if args.action == "prepare":
        return 0
    framework = str(FRAMEWORK)
    config, failure = path / "config.json", path / "failure.log"
    doctor = [framework, "doctor", str(path / "project"), "--config", str(config)]
    if args.action in ("doctor", "trial"):
        if run(doctor).returncode:
            return 1
    if args.action in ("repair", "trial"):
        return run([framework, "repair", "--project", str(path / "project"), "--config", str(config), "--failure-output", str(failure)]).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
