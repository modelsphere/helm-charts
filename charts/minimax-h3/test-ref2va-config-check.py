#!/usr/bin/env python3
"""
Tests for files/ref2va-config-check.py, the init container that refuses a server
image without the Ref2VA Turbo config protocol the chart renders for. Standard
library only:

    python3 charts/minimax-h3/test-ref2va-config-check.py

Each case builds a stand-in h3_service package declaring (or not declaring) the
protocol marker and runs the guard against it, exactly as the init container runs
it inside the server image. The server repository pins the real marker to 1
(tests/test_ref2va_protocol.py in modelsphere/minimax-h3-server).
"""
import os
import pathlib
import subprocess
import sys
import tempfile

GUARD = (pathlib.Path(__file__).resolve().parent / "files" / "ref2va-config-check.py").read_text()

# marker in h3_service/config.py -> expected exit status (None: the name is absent)
CASES = [(1, 0), (None, 1), (0, 1), (2, 1)]


def run(marker):
    with tempfile.TemporaryDirectory() as tmp:
        pkg = pathlib.Path(tmp) / "h3_service"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "config.py").write_text("" if marker is None else f"REF2VA_TURBO_CONFIG_VERSION = {marker}\n")
        return subprocess.run([sys.executable, "-c", GUARD], cwd=tmp, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": tmp})


failed = 0
for marker, expected in CASES:
    r = run(marker)
    ok = r.returncode == expected
    if expected:
        ok = ok and "Incompatible H3 Server image" in r.stderr and "both Turbo and base mode" in r.stderr
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} marker={marker!r:5} exit={r.returncode} (want {expected})")
sys.exit(1 if failed else 0)
