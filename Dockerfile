# One image for every sb-agents service (docs-agent, mt-agent, sb-support): they share
# one uv workspace and one Python, so each Deployment runs a different entry point.
# Built straight into k3s's containerd by k3s/deploy.sh (no registry, no Docker copy).

FROM ghcr.io/astral-sh/uv:0.9.5 AS uv

FROM python:3.14.0-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
# --no-editable: packages are installed into the venv, so the runtime stage needs only it.
RUN uv sync --frozen --no-dev --all-packages --no-editable

FROM python:3.14.0-slim-bookworm
# git: docs-agent indexes only git-tracked markdown (`git ls-files`).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app
COPY --from=build /app/.venv /app/.venv
# The docs corpus is a hostPath of repos owned by the Mac user, not uid 10001;
# without safe.directory git refuses them ("dubious ownership").
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYDANTIC_AI_NO_BANNER=1 \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*
USER 10001
WORKDIR /app
