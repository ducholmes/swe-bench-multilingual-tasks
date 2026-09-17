from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class RedisRunner:
    test_command: str
    language: str = "c"

    def _marked(self, command: str, test_id: str) -> list[str]:
        qid = shlex.quote(test_id)
        script = (
            f"{command}; status=$?; "
            f"if [ \"$status\" -eq 0 ]; then printf 'PASSED %s\\n' {qid}; "
            f"else printf 'FAILED %s\\n' {qid}; fi; exit \"$status\""
        )
        return ["bash", "-lc", script]

    def target_command(self, test_id: str) -> list[str]:
        # eval.sh supplies the same focused test selector used by SWE-bench.
        # `prepare` removes ignored build outputs, so rebuild the server before
        # invoking runtest.  Otherwise Redis' Tcl harness cannot find
        # src/redis-server.
        return self._marked(f"make -j$(nproc) && {self.test_command}", test_id)

    def build_command(self) -> list[str]:
        return ["bash", "-lc", "make -j$(nproc)"]

    def regression_command(self) -> list[str]:
        return ["bash", "-lc", "TERM=dumb ./runtest --durable"]
