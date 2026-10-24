"""The reference framer of spec section 6 — loaded straight from the specification text.

The code block is *executed from* ``docs/spec/LCP_Protocol_Specification.md`` instead of being
copied, so the oracle used by the differential tests can never drift from the spec.
"""

from __future__ import annotations

import re
import types
from pathlib import Path

SPEC_PATH = Path(__file__).parent.parent / "docs" / "spec" / "LCP_Protocol_Specification.md"


def _load() -> types.ModuleType:
    text = SPEC_PATH.read_text(encoding="utf-8")
    section = text[text.index("## 6.") :]
    match = re.search(r"```python\n(.*?)```", section, re.S)
    if match is None:
        raise RuntimeError("no Python code block found in section 6 of the specification")
    module = types.ModuleType("lcp_spec_reference")
    exec(compile(match.group(1), str(SPEC_PATH), "exec"), module.__dict__)
    return module


_module = _load()

StrictLCPFramer = _module.StrictLCPFramer
HEADER_SIZE: int = _module.HEADER_SIZE
MAX_PAYLOAD_LIMIT: int = _module.MAX_PAYLOAD_LIMIT
MAGIC_NUMBER: int = _module.MAGIC_NUMBER
