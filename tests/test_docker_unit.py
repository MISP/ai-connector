"""The Docker image (docker/): it carries every file the module reads at runtime and never .env.

docker/Dockerfile.dockerignore is an allow-list, so a new runtime file outside the copied
directories would be missing from the image without anyone noticing until a request fails.
The running container is checked by hand (docker/README.md).
"""

import re
from pathlib import Path

from genai import prompts

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"


def _copied() -> set[str]:
    dockerfile = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    return set(re.findall(r"^COPY (?!--)(\S+)", dockerfile, flags=re.MULTILINE))


def _allowed() -> set[str]:
    lines = (DOCKER / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    return {line[1:].rstrip("/") for line in lines if line.startswith("!")}


def test_runtime_files_are_copied_and_allowed() -> None:
    module = ROOT / "expansion" / "ai_connector.py"
    for path in (module, prompts.CLUSTERS_FILE, prompts.PINNED_TAGS_FILE):
        top = path.relative_to(ROOT).parts[0]
        assert top in _copied(), f"docker/Dockerfile does not COPY {top}"
        assert top in _allowed(), f"docker/Dockerfile.dockerignore does not allow {top}"


def test_env_never_enters_the_image() -> None:
    lines = (DOCKER / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    rules = [line for line in lines if line and not line.startswith("#")]
    assert rules[0] == "*", "the allow-list must start by excluding everything"
    assert not any(".env" in name for name in _allowed() | _copied())
