from __future__ import annotations

from dataclasses import dataclass
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
        """Run one target and emit the IDs understood by Debugging-Framework.

        The framework intentionally does not infer individual test IDs from an
        arbitrary runner's output.  Keep the real runner output, then append a
        stable, machine-readable result line for the requested test.
        """
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

    def regression_command(self) -> list[str]:
        raise NotImplementedError


class CppRunner(Runner):
    def __init__(self, test_target: str | None = None):
        super().__init__("cpp")
        self.test_target = test_target

    @staticmethod
    def target_for_test(test_id: str) -> str:
        """Map a GoogleTest ID to the CMake target containing that test.

        fmtlib uses targets such as ``format-test`` while its SWE-bench IDs
        use suites such as ``format_test``.  The Printf suite is the one older
        naming convention that needs an explicit special case.
        """
        suite = str(test_id).strip().split(".", 1)[0].split("/", 1)[0]
        if suite == "PrintfTest":
            return "printf-test"
        # fmt's ``format-test`` executable contains several GoogleTest
        # suites.  They are not individually addressable CMake targets (for
        # example fmt-2310 has no ``util-test`` target).
        if suite in {"util_test", "memory_buffer_test", "format_int_test",
                     "float_test", "uint128_test"}:
            return "format-test"
        if suite.endswith("_test"):
            return suite.replace("_", "-").lower()
        if suite.endswith("Test"):
            return suite[:-4].lower() + "-test"
        return suite.replace("_", "-").lower()

    def _test_target(self, test_id: str) -> str:
        return self.test_target or self.target_for_test(test_id)

    @staticmethod
    def _test_discovery_script(test_ids: list[str], oracle_markers: bool = False) -> str:
        """Build fmt and run IDs from the executable that actually owns them.

        Test-suite names are not CMake target names: depending on the fmt
        version, e.g. ``util_test`` may be compiled into ``format-test`` or
        another binary.  Ask each built GoogleTest executable what it owns,
        rather than relying on a version-specific naming convention.
        """
        quoted_ids = " ".join(shlex.quote(test_id) for test_id in test_ids)
        missing_marker = (
            "printf 'ORACLE_FAILED %s\\n' \"$test_id\" >&2\n    "
            if oracle_markers else ""
        )
        result_handler = (
            "if \"$binary\" --gtest_filter=\"$test_id\"; then\n"
            "    printf 'ORACLE_PASSED %s\\n' \"$test_id\"\n"
            "  else\n"
            "    printf 'ORACLE_FAILED %s\\n' \"$test_id\" >&2\n"
            "    status=1\n"
            "  fi"
            if oracle_markers else '"$binary" --gtest_filter="$test_id" || status=1'
        )
        return f'''cmake -B build -S . && cmake --build build --parallel $(nproc) || exit $?
find_gtest_binary() {{
  requested=$1
  while IFS= read -r candidate; do
    if "$candidate" --gtest_list_tests 2>/dev/null | awk '
      /^[^[:space:]]/ {{ suite=$1; sub(/\\.$/, "", suite); next }}
      /^[[:space:]]/ {{ name=$1; sub(/#.*/, "", name); print suite "." name }}
    ' | grep -Fqx "$requested"; then
      printf '%s\\n' "$candidate"
      return 0
    fi
  done < <(find build -type f -perm -111 -not -path '*/CMakeFiles/*')
  return 1
}}
status=0
for test_id in {quoted_ids}; do
  binary=$(find_gtest_binary "$test_id") || {{
    printf 'No GoogleTest executable contains %s\\n' "$test_id" >&2
    {missing_marker}status=1
    continue
  }}
  {result_handler}
done
exit "$status"'''

    def target_command(self, test_id: str) -> list[str]:
        command = ["bash", "-lc", self._test_discovery_script([test_id])]
        return self._target_with_marker(command, test_id)

    def target_group_command(self, test_ids: list[str]) -> list[str]:
        """Run an oracle group and emit one result marker per GoogleTest ID."""
        if not test_ids:
            raise ValueError("test_ids must not be empty")
        return ["bash", "-lc", self._test_discovery_script(test_ids, oracle_markers=True)]

    def build_command(self) -> list[str]:
        # The regression command runs the complete CTest suite.  Building only
        # the failing target leaves the other CTest executables absent, which
        # makes the suite report unrelated "Unable to find executable" errors
        # and causes valid repairs to be classified as noisefix.
        return [
            "bash", "-lc",
            "cmake -B build -S . && cmake --build build --parallel $(nproc)",
        ]

    def regression_command(self, skipped_tests: list[str] | None = None) -> list[str]:
        """Run CTest while excluding fixed-state-invalid GoogleTest IDs."""
        if not skipped_tests:
            return ["ctest", "--test-dir", "build", "-V"]
        test_filter = "-" + ":".join(skipped_tests)
        return [
            "bash", "-lc",
            f"GTEST_FILTER={shlex.quote(test_filter)} ctest --test-dir build -V",
        ]
