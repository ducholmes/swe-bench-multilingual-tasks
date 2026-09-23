from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class RedisRunner:
    test_command: str
    # Debugging-Framework uses one shell-safe token per requested test ID,
    # while Redis' Tcl harness uses the original test name (which may contain
    # spaces).  Keep the mapping in the generated command so the serialized
    # config remains self-contained.
    target_test_names: tuple[tuple[str, str], ...] = ()
    language: str = "c"

    @staticmethod
    def _command_without_only(command: str) -> list[str]:
        """Return the eval command's argv without its broad --only filters."""
        argv = shlex.split(command)
        result: list[str] = []
        index = 0
        while index < len(argv):
            value = argv[index]
            if value == "--only":
                if index + 1 >= len(argv):
                    raise ValueError("Redis test command has --only without a value")
                index += 2
                continue
            result.append(value)
            index += 1
        if not result:
            raise ValueError("Redis test command is empty")
        return result

    @staticmethod
    def _target_result_counts() -> str:
        """Return shell code that counts one exact Redis result marker.

        Redis appends a duration to successful records, for example::

            [ok]: Consumer Group Lag ... (6 ms)

        A fixed-string prefix is used instead of a regular expression because
        Redis test names contain characters such as ``.``, ``(``, and ``?``.
        The suffix check still prevents a similarly named test from counting.
        """
        return (
            "ok_count=$(awk -v prefix=\"[ok]: $selector\" "
            "'index($0, prefix) == 1 {"
            "suffix = substr($0, length(prefix) + 1); "
            "if (suffix == \"\" || suffix ~ /^ \\([^)]*\\)$/) count++"
            "} END {print count + 0}' \"$log\"); "
            "err_count=$(awk -v prefix=\"*** [err]: $selector in tests/\" "
            "'index($0, prefix) == 1 {count++} END {print count + 0}' \"$log\"); "
        )

    def _target_scope_command(self, test_id: str) -> str:
        """Build a command that selects exactly one Redis Tcl test.

        The target command uses the normalized framework ID and maps it back
        to Redis' original name before passing it to ``--only``.
        """
        base = "TERM=dumb " + shlex.join(self._command_without_only(self.test_command))
        names = dict(self.target_test_names)
        if test_id == "{test_id}":
            cases = "\n".join(
                f"  {shlex.quote(normalized)}) selector={shlex.quote(original)} ;;"
                for normalized, original in self.target_test_names
            )
            mapping = (
                "requested='{test_id}'; selector=\"$requested\";\n"
                "case \"$requested\" in\n"
                f"{cases}\n"
                "esac;\n"
            )
            marker = '"$requested"'
        else:
            original = names.get(test_id, test_id)
            mapping = (
                f"requested={shlex.quote(test_id)}; "
                f"selector={shlex.quote(original)};\n"
            )
            marker = shlex.quote(test_id)

        # Redis exits successfully when --only matches no test because every
        # test is simply skipped.  Capture the output and require exactly one
        # [ok] or [err] record for the requested selector.
        return mapping + (
            f"log=$(mktemp); {base} --only \"$selector\" >\"$log\" 2>&1; "
            "status=$?; cat \"$log\"; "
            + self._target_result_counts()
            # Redis prints each failure twice: once when it runs and once in
            # the final `*** [err]` summary.  Count only the summary record so
            # one failed test is not mistaken for an invalid target.
            + "executed=$((ok_count + err_count)); "
            f"if [ \"$executed\" -ne 1 ]; then printf 'INVALID_TARGET %s\\n' {marker} >&2; status=1; fi; "
            "if [ \"$ok_count\" -ne 1 ] || [ \"$err_count\" -ne 0 ]; then status=1; fi; "
            "rm -f \"$log\"; test \"$status\" -eq 0"
        )

    def oracle_command(self, test_ids: list[str]) -> list[str]:
        """Run each declared test exactly once and emit per-ID markers."""
        if not test_ids:
            raise ValueError("test_ids must not be empty")

        base = "TERM=dumb " + shlex.join(self._command_without_only(self.test_command))
        cases = "\n".join(
            f"  {shlex.quote(normalized)}) selector={shlex.quote(original)} ;;"
            for normalized, original in self.target_test_names
        )
        requested = " ".join(shlex.quote(test_id) for test_id in test_ids)
        script = (
            "make -j$(nproc) || exit $?\n"
            "status=0\n"
            f"for requested in {requested}; do\n"
            "  selector=\"$requested\"\n"
            "  case \"$requested\" in\n"
            f"{cases}\n"
            "  esac\n"
            "  log=$(mktemp)\n"
            f"  {base} --only \"$selector\" >\"$log\" 2>&1\n"
            "  test_status=$?\n"
            "  cat \"$log\"\n"
            + "  " + self._target_result_counts() + "\n"
            + "  # Redis emits a live error and repeats it in the final summary.\n"
            + "  executed=$((ok_count + err_count))\n"
            "  if [ \"$executed\" -ne 1 ]; then\n"
            "    printf 'ORACLE_INVALID %s\\n' \"$requested\" >&2\n"
            "    status=1\n"
            "  elif [ \"$test_status\" -eq 0 ] && [ \"$ok_count\" -eq 1 ] && [ \"$err_count\" -eq 0 ]; then\n"
            "    printf 'ORACLE_PASSED %s\\n' \"$requested\"\n"
            "  else\n"
            "    printf 'ORACLE_FAILED %s\\n' \"$requested\" >&2\n"
            "    status=1\n"
            "  fi\n"
            "  rm -f \"$log\"\n"
            "done\n"
            "exit \"$status\""
        )
        return ["bash", "-lc", script]

    def _marked(self, command: str, test_id: str) -> list[str]:
        qid = shlex.quote(test_id)
        script = (
            f"{command}; status=$?; "
            f"if [ \"$status\" -eq 0 ]; then printf 'PASSED %s\\n' {qid}; "
            f"else printf 'FAILED %s\\n' {qid}; fi; exit \"$status\""
        )
        return ["bash", "-lc", script]

    def target_command(self, test_id: str) -> list[str]:
        # `prepare` removes ignored build outputs, so rebuild the server before
        # invoking runtest.  Otherwise Redis' Tcl harness cannot find
        # src/redis-server.  Replace eval.sh's broad selector with an exact
        # Tcl test name.
        return self._marked(
            f"make -j$(nproc) && {self._target_scope_command(test_id)}", test_id
        )

    def build_command(self) -> list[str]:
        return ["bash", "-lc", "make -j$(nproc)"]

    def regression_command(
        self,
        pass_to_pass_tests: list[str],
        skipped_tests: list[str] | None = None,
    ) -> list[str]:
        """Run exactly the PASS_TO_PASS tests declared by the task."""
        skipped = set(skipped_tests or [])
        test_ids = [test for test in pass_to_pass_tests if test not in skipped]
        if not test_ids:
            raise ValueError("no eligible PASS_TO_PASS tests remain after oracle exclusions")

        base = "TERM=dumb " + shlex.join(
            self._command_without_only(self.test_command)
        )
        selectors = " ".join(shlex.quote(test) for test in test_ids)
        script = (
            "make -j$(nproc) || exit $?\n"
            "status=0\n"
            f"for selector in {selectors}; do\n"
            "  log=$(mktemp)\n"
            f"  {base} --only \"$selector\" >\"$log\" 2>&1\n"
            "  test_status=$?\n"
            "  cat \"$log\"\n"
            + "  " + self._target_result_counts() + "\n"
            "  executed=$((ok_count + err_count))\n"
            "  if [ \"$executed\" -ne 1 ]; then\n"
            "    printf 'REGRESSION_INVALID %s\\n' \"$selector\" >&2\n"
            "    status=1\n"
            "  elif [ \"$test_status\" -eq 0 ] && [ \"$ok_count\" -eq 1 ] && [ \"$err_count\" -eq 0 ]; then\n"
            "    printf 'REGRESSION_PASSED %s\\n' \"$selector\"\n"
            "  else\n"
            "    printf 'REGRESSION_FAILED %s\\n' \"$selector\" >&2\n"
            "    status=1\n"
            "  fi\n"
            "  rm -f \"$log\"\n"
            "done\n"
            "exit \"$status\""
        )
        return ["bash", "-lc", script]
