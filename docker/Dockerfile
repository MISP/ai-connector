# The ai_connector module in a misp-modules server on port 6667. Build from the
# repository root: docker compose -f docker/docker-compose.yml build
FROM python:3.14-slim@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy

WORKDIR /app

# The versions and hashes pinned in uv.lock; the e2e extra is misp-modules, the server.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra e2e --no-install-project

# What the module reads at runtime; Dockerfile.dockerignore keeps everything else out, .env too.
COPY expansion expansion
COPY genai genai
COPY clusters clusters

RUN useradd --uid 1000 --user-group app
USER app

EXPOSE 6667
HEALTHCHECK CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6667/healthcheck', timeout=5)"]
CMD ["/app/.venv/bin/python", "-m", "misp_modules", "-c", "/app", "-l", "0.0.0.0", "-p", "6667"]
