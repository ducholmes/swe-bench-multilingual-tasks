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
        if suite.endswith("_test"):
            return suite.replace("_", "-").lower()
        if suite.endswith("Test"):
            return suite[:-4].lower() + "-test"
        return suite.replace("_", "-").lower()

    def _test_target(self, test_id: str) -> str:
        return self.test_target or self.target_for_test(test_id)

    def target_command(self, test_id: str) -> list[str]:
        # Build the test binary, then pass the SWE-bench GoogleTest id to it.
        # Running only the executable is important: ctest's `-R` selects the
        # test binary, but does not select an individual GoogleTest case.
        target = shlex.quote(self._test_target(test_id))
        command = [
            "bash", "-lc",
            "cmake -B build -S . && "
            f"cmake --build build --target {target} && "
            f"binary=$(find build -type f -name {target} -perm -111 -print -quit) && "
            "test -n \"$binary\" && "
            f'\"$binary\" --gtest_filter={shlex.quote(test_id)}',
        ]
        return self._target_with_marker(command, test_id)

    def build_command(self) -> list[str]:
        # The regression command runs the complete CTest suite.  Building only
        # the failing target leaves the other CTest executables absent, which
        # makes the suite report unrelated "Unable to find executable" errors
        # and causes valid repairs to be classified as noisefix.
        return [
            "bash", "-lc",
            "cmake -B build -S . && cmake --build build --parallel $(nproc)",
        ]

    def regression_command(self) -> list[str]:
        return ["ctest", "--test-dir", "build", "-V"]
