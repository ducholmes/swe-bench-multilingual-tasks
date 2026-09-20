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
It builds the CMake project and discovers the GoogleTest executable that owns
each declared ID via `--gtest_list_tests`; this avoids assuming that a suite
name is also a CMake target name. It then runs the failing test to create
`failure.log`, cleans generated build files, and writes a schema-version-6
config.

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
