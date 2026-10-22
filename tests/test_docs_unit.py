"""The documented ai_connector server port is 6667, never the stock misp-modules port 6666.

misp-modules defaults `-p` to 6666, the port of the stock enrichment server every MISP
installation already runs, and MISP's `Plugin.AI_services_port` defaults to 6666 as well.
The AI server therefore gets its own port, and every copy-pasteable command in the current
guides must say so (docs/OPERATOR_GUIDE.md 1.4). Historical records are not checked.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CURRENT_GUIDES = ["README.md", "CLAUDE.md", "docs/OPERATOR_GUIDE.md", "docs/DEVELOPER_GUIDE.md"]
STOCK_PORT = re.compile(r"(?::|-p )6666\b")


@pytest.mark.parametrize("name", CURRENT_GUIDES)
def test_documented_server_port_is_6667(name: str) -> None:
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not STOCK_PORT.search(text), f"{name} still uses the stock misp-modules port 6666"
    assert "-p 6667" in text or ":6667" in text, f"{name} does not document port 6667"
