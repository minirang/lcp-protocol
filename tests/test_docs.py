"""Documentation must not rot: referenced tests exist, links resolve, README code runs."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    ROOT / "README.md",
    ROOT / "README.en.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    *sorted((ROOT / "docs").glob("*.md")),
]


def test_every_test_named_in_the_docs_exists() -> None:
    functions: set[str] = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        functions |= set(re.findall(r"^(?:async )?def (test_\w+)\(", path.read_text(), re.M))
    modules = {path.stem for path in (ROOT / "tests").glob("test_*.py")}
    missing = [
        (doc.name, name)
        for doc in DOCS
        for name in sorted(set(re.findall(r"\btest_[a-z0-9_]+", doc.read_text(encoding="utf-8"))))
        if name not in functions | modules
    ]
    assert not missing, f"docs mention tests that do not exist: {missing}"


def test_relative_links_resolve() -> None:
    broken = []
    for doc in DOCS:
        for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not (doc.parent / target.split("#")[0]).resolve().exists():
                broken.append((doc.name, target))
    assert not broken, f"broken links: {broken}"


@pytest.mark.parametrize(
    ("readme", "marker"),
    [
        ("README.md", "import asyncio"),
        ("README.md", "from lcp import Frame, FrameDecoder"),
        ("README.en.md", "import asyncio"),
    ],
)
def test_readme_snippets_run(readme: str, marker: str, tmp_path: Path) -> None:
    blocks = re.findall(r"```python\n(.*?)```", (ROOT / readme).read_text(encoding="utf-8"), re.S)
    snippet = next(block for block in blocks if block.startswith(marker))
    script = tmp_path / "snippet.py"
    script.write_text(snippet, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
