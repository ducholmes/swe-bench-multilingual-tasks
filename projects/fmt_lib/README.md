# Debugging-Framework inputs for SWE-bench Multilingual

`run_debugging_cases.py` materializes a SWE-bench task as the independent
Debugging-Framework input contract:

```text
debugging/out/<instance-id>/
├── config.json
├── failure.log
└── <instance-id>/ # Git checkout copied from the SWE-bench image
```

For all `fmtlib/fmt` instances, prepare the inputs with:

```bash
cd /home/anh_duc/projects/APR/swe-bench-multilingual-tasks
python debugging/run_debugging_cases.py prepare --all --build
```

The script reads each task's image, base commit, `test.patch`, and
`FAIL_TO_PASS` and `PASS_TO_PASS` IDs. Before producing an input it verifies
the oracle: every `FAIL_TO_PASS` test must fail on the buggy checkout, then
the source hunks of `gold.patch` are applied to a disposable copy (the test
hunks are already supplied by `test.patch`) and all declared tests must pass.
The CMake build and CTest commands come from that task's `eval.sh`. Each CTest
run is restricted with `GTEST_FILTER` to one ID from `tests.json`, so tests
outside the declared oracle cannot affect repair validation. Each selected test
has a 30-second timeout and one retry, and command output is streamed while
prepare runs. The script then runs the failing test to create `failure.log`,
cleans generated build files, and writes a schema-version-6 config whose
regression command runs exactly the declared `FAIL_TO_PASS` and `PASS_TO_PASS`
IDs, excluding any fixed-state-invalid regression IDs found by the oracle
check. On macOS, patches are applied from
inside the task container so Docker Desktop cannot build against a stale
pre-patch file size; synthetic executable-bit changes from `docker cp` are
also ignored.

Run validation for every prepared instance:

```bash
python debugging/run_debugging_cases.py doctor --all
```

Run Debugging-Framework for every instance sequentially:

```bash
python debugging/run_debugging_cases.py repair --all
```

Use `--instance-id fmtlib__fmt-2457` instead of `--all` to work on one case.
Pass `--build` to build each task's Docker image from its task-local
`Dockerfile` before preparing it. Without `--build`, the image must already
exist locally.
