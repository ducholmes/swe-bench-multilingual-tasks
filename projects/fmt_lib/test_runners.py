from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

from run_debugging_cases import complete_oracle_results
from runners import CppRunner


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tasks"


class CppRunnerTest(unittest.TestCase):
    def runner(self) -> CppRunner:
        return CppRunner.from_eval_script(TASKS / "fmtlib__fmt-3863" / "eval.sh")

    def test_all_fmt_eval_scripts_have_supported_commands(self) -> None:
        scripts = sorted(TASKS.glob("fmtlib__fmt-*/eval.sh"))
        self.assertTrue(scripts)
        for script in scripts:
            with self.subTest(script=script):
                runner = CppRunner.from_eval_script(script)
                self.assertEqual(runner.test_command[0], "ctest")
                self.assertIn("cmake -B build -S .", runner.build_command()[2])
                self.assertIn("cmake --build build", runner.build_command()[2])

    def test_regression_uses_eval_ctest_and_declared_tests_only(self) -> None:
        command = self.runner().regression_command(
            ["suite.failing", "suite.passing", "suite.passing"],
            ["suite.passing"],
        )

        self.assertEqual(command[:2], ["bash", "-lc"])
        script = command[2]
        self.assertIn("for test_id in suite.failing; do", script)
        self.assertNotIn("suite.passing", script)
        self.assertIn(
            'GTEST_FILTER="$test_id" timeout -k 5s 30s '
            'ctest --test-dir build -V -R ranges-test',
            script,
        )
        self.assertIn('while [ "$attempt" -le 2 ]', script)
        self.assertIn("[retry] %s timed out", script)
        self.assertIn('grep -Fq "[ RUN      ] $test_id"', script)
        self.assertIn("printf 'PASSED %s\\n'", script)
        self.assertIn("printf 'FAILED %s\\n'", script)

    def test_regression_rejects_an_empty_selected_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.runner().regression_command(["suite.test"], ["suite.test"])

    def test_interrupted_oracle_is_rejected(self) -> None:
        result = subprocess.CompletedProcess(
            [], 137, stdout="ORACLE_PASSED suite.one\nORACLE_FAILED suite.two\n"
        )
        with self.assertRaisesRegex(RuntimeError, "missing: suite.three"):
            complete_oracle_results(
                result, ["suite.one", "suite.two", "suite.three"]
            )


if __name__ == "__main__":
    unittest.main()
