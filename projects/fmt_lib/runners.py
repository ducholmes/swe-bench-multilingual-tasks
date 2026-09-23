from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex


@dataclass(frozen=True)
class Runner:
    language: str

    def normalize_test_id(self, test_id: str) -> str:
        return str(test_id).strip()

    def target_command(self, test_id: str) -> list[str]:
        raise NotImplementedError

    @staticmethod
    def _target_with_marker(command: list[str], test_id: str) -> list[str]:
        """Run one target and emit the IDs understood by Debugging-Framework."""
        command_line = shlex.join(command)
        quoted_id = shlex.quote(test_id)
        script = (
            f"{command_line}; status=$?; "
            f"if [ \"$status\" -eq 0 ]; then "
            f"printf 'PASSED %s\\n' {quoted_id}; "
            f"else printf 'FAILED %s\\n' {quoted_id}; fi; "
            "exit \"$status\""
        )
        return ["bash", "-lc", script]

    def build_command(self) -> list[str]:
        return []

    def regression_command(
        self, test_ids: list[str], skipped_tests: list[str] | None = None
    ) -> list[str]:
        raise NotImplementedError


class CppRunner(Runner):
    TEST_TIMEOUT_SECONDS = 30
    TEST_TIMEOUT_KILL_AFTER_SECONDS = 5
    TEST_ATTEMPTS = 2

    def __init__(
        self,
        configure_line: str,
        build_line: str,
        test_command: tuple[str, ...],
    ):
        super().__init__("cpp")
        self.configure_line = configure_line
        self.build_line = build_line
        self.test_command = test_command

    @classmethod
    def from_eval_script(cls, eval_script: Path) -> CppRunner:
        """Read the CMake/CTest contract declared by a task's ``eval.sh``."""
        lines = [line.strip() for line in eval_script.read_text().splitlines()]
        configure_lines = [line for line in lines if line.startswith("cmake -B ")]
        build_lines = [line for line in lines if line.startswith("cmake --build ")]
        test_lines = []
        for line in lines:
            match = re.fullmatch(r"\((ctest\s+.*?)\)\s*\|\s*cat", line)
            if match:
                test_lines.append(match.group(1))
        if len(configure_lines) != 1 or len(build_lines) != 1 or len(test_lines) != 1:
            raise ValueError(
                f"Expected one configure, build, and CTest command in {eval_script}"
            )
        test_command = tuple(shlex.split(test_lines[0]))
        if not test_command or test_command[0] != "ctest":
            raise ValueError(f"Unsupported test command in {eval_script}: {test_lines[0]}")
        return cls(configure_lines[0], build_lines[0], test_command)

    def _filtered_test_script(
        self, test_ids: list[str], marker_prefix: str | None = None
    ) -> str:
        """Run the task's CTest command, limited to IDs from ``tests.json``."""
        if not test_ids:
            raise ValueError("test_ids must not be empty")
        quoted_ids = " ".join(shlex.quote(test_id) for test_id in test_ids)
        test_line = shlex.join(self.test_command)
        timeout_line = (
            f"timeout -k {self.TEST_TIMEOUT_KILL_AFTER_SECONDS}s "
            f"{self.TEST_TIMEOUT_SECONDS}s {test_line}"
        )
        result_handler = (
            "if run_selected \"$test_id\"; then\n"
            f"    printf '{marker_prefix}PASSED %s\\n' \"$test_id\"\n"
            "  else\n"
            f"    printf '{marker_prefix}FAILED %s\\n' \"$test_id\" >&2\n"
            "    status=1\n"
            "  fi"
            if marker_prefix is not None
            else 'run_selected "$test_id" || status=1'
        )
        return f'''run_selected() {{
  test_id=$1
  attempt=1
  while [ "$attempt" -le {self.TEST_ATTEMPTS} ]; do
    printf '[test] %s (attempt %s/{self.TEST_ATTEMPTS})\n' "$test_id" "$attempt"
    output=$(GTEST_FILTER="$test_id" {timeout_line} 2>&1)
    command_status=$?
    printf '%s\n' "$output"
    if [ "$command_status" -ne 124 ] && [ "$command_status" -ne 137 ]; then
      break
    fi
    if [ "$attempt" -eq {self.TEST_ATTEMPTS} ]; then
      break
    fi
    printf '[retry] %s timed out\n' "$test_id" >&2
    attempt=$((attempt + 1))
  done
  if [ "$command_status" -ne 0 ]; then
    return "$command_status"
  fi
  # GoogleTest returns zero even when a filter selects no tests. Require proof
  # that this exact SWE-bench ID was executed.
  printf '%s\n' "$output" | grep -Fq "[ RUN      ] $test_id"
}}
status=0
for test_id in {quoted_ids}; do
  {result_handler}
done
exit "$status"'''

    def target_command(self, test_id: str) -> list[str]:
        command = ["bash", "-lc", self._filtered_test_script([test_id])]
        return self._target_with_marker(command, test_id)

    def target_group_command(self, test_ids: list[str]) -> list[str]:
        """Run an oracle group and emit one result marker per GoogleTest ID."""
        return [
            "bash", "-lc",
            self._filtered_test_script(test_ids, marker_prefix="ORACLE_"),
        ]

    def build_command(self) -> list[str]:
        return [
            "bash", "-lc",
            f"{self.configure_line} && {self.build_line}",
        ]

    def regression_command(
        self, test_ids: list[str], skipped_tests: list[str] | None = None
    ) -> list[str]:
        """Run exactly the task-declared regression oracle."""
        skipped = set(skipped_tests or [])
        selected = [
            test_id for test_id in dict.fromkeys(test_ids)
            if test_id and test_id not in skipped
        ]
        if not selected:
            raise ValueError("regression test set must not be empty")
        return [
            "bash", "-lc",
            self._filtered_test_script(selected, marker_prefix=""),
        ]
