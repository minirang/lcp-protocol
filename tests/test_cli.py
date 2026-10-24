from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lcp import __version__
from lcp.cli import main, parse_hex

ROOT = Path(__file__).resolve().parent.parent
REQ_HEX = "4c43 01 01 00 00 000b 00000001 00000007 02 0008 2f76312f6563686f 7b2261223a317d"


def test_parse_hex_accepts_common_notations() -> None:
    assert parse_hex("4c 43") == parse_hex("4c43") == parse_hex("0x4c,0x43") == b"LC"
    assert parse_hex("4C:43\n") == b"LC"
    with pytest.raises(ValueError):
        parse_hex("zz")


def test_inspect_decodes_a_frame(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", REQ_HEX]) == 0
    out = capsys.readouterr().out
    for expected in (
        "Frame #1",
        "REQ (0x01)",
        "JSON (0x00)",
        "OK (0x00)",
        "correlation id  1",
        "TARGET_PATH",
        "/v1/echo",
        '{"a":1}',
        "magic           0x4C43 ('LC')",
    ):
        assert expected in out


def test_inspect_reads_stdin_and_reports_trailing_bytes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(REQ_HEX + " 4c43 01"))
    assert main(["inspect", "-"]) == 0
    out = capsys.readouterr().out
    assert "Frame #1" in out and "3 trailing bytes" in out


def test_inspect_reports_protocol_violations(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", "4745 5420 2f20 4854 5450 2f31 2e31 0d0a"]) == 1
    assert "protocol violation" in capsys.readouterr().err


def test_encode_then_inspect_roundtrip_and_token_redaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = [
        "encode",
        "req",
        "--cid",
        "9",
        "--path",
        "/x",
        "--token",
        "hunter2",
        "--json",
        '{"k":"v"}',
    ]
    assert main(args) == 0
    wire_hex = capsys.readouterr().out.strip()
    assert wire_hex.startswith("4c 43 01 01")
    assert main(["inspect", wire_hex]) == 0
    out = capsys.readouterr().out
    assert "correlation id  9" in out and '{"k":"v"}' in out
    assert "<redacted>" in out and "hunter2" not in out  # tokens are never echoed by the tool


def test_encode_ping_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["encode", "ping", "--cid", "7"]) == 0
    assert capsys.readouterr().out.strip() == "4c 43 01 05 00 00 00 00 00 00 00 07 00 00 00 00"
    assert main(["encode", "ping", "--text", "payload"]) == 1  # PING cannot carry a payload
    assert "error" in capsys.readouterr().err
    assert main(["encode", "req", "--json", "{broken"]) == 1


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_serve_request_and_ping_end_to_end() -> None:
    server = subprocess.Popen(
        [sys.executable, "-m", "lcp", "serve", "--port", "0", "--token", "s3cret"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert server.stdout is not None
        banner = server.stdout.readline()
        match = re.search(r":(\d+) ", banner)
        assert match, banner
        port = match.group(1)

        def lcp(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "lcp", *args, "--port", port],
                capture_output=True,
                text=True,
                timeout=30,
            )

        echoed = lcp("request", "/v1/echo", "--json", '{"hello": "cli"}', "--token", "s3cret")
        assert echoed.returncode == 0, echoed.stderr
        assert "OK (0x00)" in echoed.stdout and '"hello": "cli"' in echoed.stdout

        denied = lcp("request", "/v1/echo", "--json", "{}")
        assert denied.returncode == 1 and "UNAUTHORIZED" in denied.stdout

        missing = lcp("request", "/nope", "--token", "s3cret")
        assert missing.returncode == 1 and "NOT_FOUND" in missing.stdout

        now = lcp("request", "/v1/time", "--token", "s3cret")
        assert now.returncode == 0 and '"time"' in now.stdout

        pong = lcp("ping", "-c", "2")
        assert pong.returncode == 0 and pong.stdout.count("PONG") == 2
    finally:
        server.terminate()
        server.wait(timeout=15)
        for stream in (server.stdout, server.stderr):
            if stream is not None:
                stream.close()


def test_mutual_tls_through_the_cli(tmp_path: Path) -> None:
    pytest.importorskip("trustme")
    certs = tmp_path / "certs"
    gen = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_dev_certs.py"), str(certs)],
        capture_output=True,
        text=True,
    )
    assert gen.returncode == 0, gen.stderr
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lcp",
            "serve",
            "--port",
            "0",
            "--tls-cert",
            str(certs / "server.pem"),
            "--client-ca",
            str(certs / "ca.pem"),
            "--require-client-cert",
        ],  # fmt: skip
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert server.stdout is not None
        banner = server.stdout.readline()
        match = re.search(r":(\d+) \(TLS\)", banner)
        assert match, banner
        base = ["--port", match.group(1), "--ca", str(certs / "ca.pem")]

        def lcp(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "lcp", *args], capture_output=True, text=True, timeout=30
            )

        ok = lcp(
            "request",
            "/v1/echo",
            "--json",
            '{"tls": true}',
            *base,
            "--cert",
            str(certs / "client.pem"),
        )
        assert ok.returncode == 0, ok.stderr
        assert '"tls": true' in ok.stdout
        anonymous = lcp("request", "/v1/echo", "--json", "{}", *base)  # no client certificate
        assert anonymous.returncode == 1
    finally:
        server.terminate()
        server.wait(timeout=15)
        for stream in (server.stdout, server.stderr):
            if stream is not None:
                stream.close()
