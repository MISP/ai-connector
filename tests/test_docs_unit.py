"""What the current guides must say about installing and running the ai_connector server.

- The server port is 6667, never the stock misp-modules port 6666: misp-modules defaults `-p`
  to 6666, the port of the stock enrichment server every MISP installation already runs, and
  MISP's `Plugin.AI_services_port` defaults to 6666 as well. The AI server therefore gets its
  own port, and every copy-pasteable command in the current guides must say so
  (docs/OPERATOR_GUIDE.md 1.4).
- The from-scratch install (uv installer, clone of the canonical repository, Python 3.14 via
  uv) is written once, in docs/OPERATOR_GUIDE.md 1.2; the other guides point there instead of
  repeating it, and none of them clones from the old ctitools remote. docs/INSTALL.md is the
  exception: its systemd install for a MISP host is a recipe of its own (system user, /opt,
  no dev extras), not a copy of 1.2.
- MISP displays docs/INSTALL.md: it downloads it from `main`, turns each "## " section into a
  tab and opens "Docker" or "Standard install" by name, so those sections must stay, and no
  "## " line may sit inside a code block, where it would split a tab.

Historical records (CHANGELOG, INTEGRATION_PLAN, the upstream snippets in ARCHITECTURE) are
not checked.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_GUIDE = "docs/OPERATOR_GUIDE.md"
MISP_GUIDE = "docs/INSTALL.md"
MISP_TABS = {"Docker", "Standard install", "LLM endpoint", "Troubleshooting"}
CURRENT_GUIDES = ["README.md", "CLAUDE.md", INSTALL_GUIDE, "docs/DEVELOPER_GUIDE.md", MISP_GUIDE]
STOCK_PORT = re.compile(r"(?::|-p )6666\b")
FROM_SCRATCH = [
    "curl -LsSf https://astral.sh/uv/install.sh | sh",
    "git clone https://github.com/MISP/ai-connector.git && cd ai-connector",
    "uv venv --python 3.14 .venv",
    'uv pip install --python .venv/bin/python -e ".[dev,e2e]"',
]


@pytest.mark.parametrize("name", CURRENT_GUIDES)
def test_documented_server_port_is_6667(name: str) -> None:
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not STOCK_PORT.search(text), f"{name} still uses the stock misp-modules port 6666"
    assert "-p 6667" in text or ":6667" in text, f"{name} does not document port 6667"


def test_from_scratch_install_is_written_once() -> None:
    guide = (ROOT / INSTALL_GUIDE).read_text(encoding="utf-8")
    for line in FROM_SCRATCH:
        assert line in guide, f"{INSTALL_GUIDE} lacks the install line {line!r}"
    for name in CURRENT_GUIDES:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "ctitools/ai-connector" not in text, f"{name} clones from the old remote"
        if name not in (INSTALL_GUIDE, MISP_GUIDE):
            assert "astral.sh" not in text, f"{name} repeats the install instead of linking 1.2"


def test_misp_guide_keeps_the_tabs_misp_shows() -> None:
    headings, fenced = [], False
    for line in (ROOT / MISP_GUIDE).read_text(encoding="utf-8").splitlines():
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        elif line.startswith("## "):
            assert not fenced, f"{MISP_GUIDE}: {line!r} inside a code block would split a tab"
            headings.append(line[3:].strip())
    missing = MISP_TABS - set(headings)
    assert not missing, f"{MISP_GUIDE} lost a tab MISP shows: {missing}"
