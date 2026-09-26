# The factory controller image: runs `factory serve` or `factory worker`
# (build spec §3 B7). Same image, different `command:` in docker-compose.yml.
#
# This image holds the controller's OWN code and a `claude` CLI for
# ClaudeRuntime's environment-scrubbing wrapper (final draft §12.3). It never
# holds git, deploy or production credentials -- those live only in the
# GitHub Apps / deploy hooks the controller calls out to, per §13.1 #1/#5/#9.
FROM python:3.12-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# uv: the project's own package/dependency manager (pyproject.toml, uv.lock).
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /usr/local/bin/

# The Claude CLI, for `ClaudeRuntime`'s environment-scrubbing wrapper. Pin a
# version in production; `--force` keeps the build reproducible if the base
# image's npm/node version drifts. If your environment already has an
# internal mirror for this, point npm at it via a build arg instead.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*
ENV FACTORY_CLAUDE_CLI_PATH=/usr/bin/claude

RUN useradd --create-home --uid 1000 --shell /bin/bash factory
WORKDIR /app

COPY factory-controller/pyproject.toml factory-controller/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY factory-controller/src ./src
COPY factory-controller/README.md ./
RUN uv sync --frozen --no-dev

# The factory-kit (agent prompts, schemas, policies) and the product configs
# it maps repos to are mounted read-only at runtime (see docker-compose.yml):
# they change independently of the controller image, under the kit gate
# (final draft §13.3), not a container rebuild.
ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /var/lib/factory/sandboxes /var/lib/factory/repos /var/lib/factory/evidence \
    && chown -R factory:factory /var/lib/factory /app
USER factory

ENV FACTORY_MODE=production \
    FACTORY_SANDBOX_ROOT=/var/lib/factory/sandboxes \
    FACTORY_REPOS_ROOT=/var/lib/factory/repos \
    FACTORY_EVIDENCE_DIR=/var/lib/factory/evidence

EXPOSE 8080

# docker-compose.yml overrides this per service (`factory serve` / `factory worker`).
CMD ["factory", "serve", "--host", "0.0.0.0", "--port", "8080"]
