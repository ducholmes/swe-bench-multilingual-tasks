from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Runner:
    language: str

    def target_command(self, test_id: str) -> list[str]:
        raise NotImplementedError

    def build_command(self) -> list[str]:
        return []

    def regression_command(self) -> list[str]:
        raise NotImplementedError


class CppRunner(Runner):
    def __init__(self):
        super().__init__("cpp")

    def target_command(self, test_id: str) -> list[str]:
        return ["bash", "-lc", "cmake -B build -S . && cmake --build build --target ranges-test && ctest --test-dir build -V -R ranges-test"]

    def build_command(self) -> list[str]:
        return ["cmake", "-B", "build", "-S", "."]

    def regression_command(self) -> list[str]:
        return ["ctest", "--test-dir", "build", "-V"]


class JavaRunner(Runner):
    def __init__(self, module: str | None = None):
        super().__init__("java")
        self.module = module

    def target_command(self, test_id: str) -> list[str]:
        # SWE-bench Maven IDs use either Class#method or Class > method.
        test_id = test_id.replace(" > ", "#")
        command = ["mvn", "test", "-B"]
        if self.module:
            command.extend(["-pl", self.module])
        command.append(f"-Dtest={test_id}")
        return command

    def build_command(self) -> list[str]:
        command = ["mvn", "compile", "-B", "-DskipTests"]
        if self.module:
            command.extend(["-pl", self.module])
        return command

    def regression_command(self) -> list[str]:
        command = ["mvn", "test", "-B"]
        if self.module:
            command.extend(["-pl", self.module])
        return command


class RustRunner(Runner):
    def __init__(self):
        super().__init__("rust")

    def target_command(self, test_id: str) -> list[str]:
        # Cargo accepts a substring filter and this preserves module paths.
        return ["cargo", "test", test_id]

    def build_command(self) -> list[str]:
        return ["cargo", "test", "--no-run"]

    def regression_command(self) -> list[str]:
        return ["cargo", "test"]


class PythonRunner(Runner):
    def __init__(self, micropython: bool = False):
        super().__init__("python")
        self.micropython = micropython

    def target_command(self, test_id: str) -> list[str]:
        if self.micropython:
            # MicroPython's Docker image provides the unix port.
            return ["bash", "-lc", f"make -C ports/unix -j2 && ports/unix/micropython tests/{test_id}"]
        return ["python", "-m", "pytest", test_id]

    def build_command(self) -> list[str]:
        if self.micropython:
            return ["make", "-C", "ports/unix", "-j2"]
        return []

    def regression_command(self) -> list[str]:
        if self.micropython:
            return ["bash", "-lc", "make -C ports/unix -j2 && make -C tests"]
        return ["python", "-m", "pytest"]


def runner_for(language: str, *, micropython: bool = False, module: str | None = None) -> Runner:
    if language in ("c", "cpp"):
        return CppRunner()
    if language == "java":
        return JavaRunner(module=module)
    if language == "rust":
        return RustRunner()
    if language == "python":
        return PythonRunner(micropython=micropython)
    raise ValueError(f"Unsupported language: {language}")
