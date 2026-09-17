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
python debugging/run_debugging_cases.py prepare --all
```

The script reads each task's image, base commit, `test.patch`, and
`FAIL_TO_PASS` IDs. It derives the CMake test target from the GoogleTest suite
(`printf-test`, `format-test`, `ranges-test`, `xchar-test`, or `std-test`),
runs the failing test to create `failure.log`, cleans generated build files,
and writes a schema-version-6 config.

Run validation for every prepared instance:

```bash
python debugging/run_debugging_cases.py doctor --all
```

Run Debugging-Framework for every instance sequentially:

```bash
python debugging/run_debugging_cases.py repair --all
```

Use `--instance-id fmtlib__fmt-2457` instead of `--all` to work on one case.
The Docker images must already exist locally; the script does not build or
pull them.
