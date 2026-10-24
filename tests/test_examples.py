"""The documented examples, scripts and benchmark harness must keep working."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    "01_echo.py": "NOT_FOUND",
    "02_events.py": "client got push",
    "03_auth_jwt.py": "expired  token -> UNAUTHORIZED",
    "04_backpressure.py": "all retried automatically",
    "05_tls_mtls.py": "TLSv1.3",
    "06_raw_frames.py": "Frame #1",
}


def run(*args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, timeout=timeout, cwd=ROOT
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_example_runs(name: str) -> None:
    if name.startswith("05"):
        pytest.importorskip("trustme")
    result = run(f"examples/{name}")
    assert result.returncode == 0, result.stderr
    assert EXPECTED[name] in result.stdout


def test_every_example_is_covered() -> None:
    assert {p.name for p in (ROOT / "examples").glob("0*.py")} == set(EXPECTED)


def test_dev_cert_script(tmp_path: Path) -> None:
    pytest.importorskip("trustme")
    result = run("scripts/gen_dev_certs.py", str(tmp_path / "certs"))
    assert result.returncode == 0, result.stderr
    for name in ("ca.pem", "server.pem", "client.pem"):
        assert (tmp_path / "certs" / name).read_text().startswith("-----BEGIN")


def test_benchmark_harness_smoke(tmp_path: Path) -> None:
    out = tmp_path / "results.json"
    result = run(
        "-m", "benchmarks.bench", "--sizes", "100", "--concurrency", "2",
        "--duration", "0.4", "--warmup", "0.1", "--json", str(out),
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    results = json.loads(out.read_text())
    assert {r["protocol"] for r in results} == {"lcp", "lcp-raw", "http1"}
    assert all(r["tps"] > 0 and r["p99_ms"] >= r["p50_ms"] > 0 for r in results)
