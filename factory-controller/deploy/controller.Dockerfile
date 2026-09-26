# The factory controller image: runs `factory serve` or `factory worker`
# (build spec §3 B7). Same image, different `command:` in docker-compose.yml.
#
# This image holds the controller's OWN code and a `claude` CLI for
# ClaudeRuntime's environment-scrubbing wrapper (final draft §12.3). It never
# holds git, deploy or production credentials -- those live only in the
# GitHub Apps / deploy hooks the controller calls out to, per §13.1 #1/#5/#9.
FROM node:22-slim AS node

FROM python:3.12-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# uv: the project's own package/dependency manager (pyproject.toml, uv.lock).
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /usr/local/bin/
# Generous download timeout: large wheels on slow links otherwise fail the build.
ENV UV_HTTP_TIMEOUT=300

# The Claude CLI, for `ClaudeRuntime`'s environment-scrubbing wrapper. Pinned
# to an exact version so a rebuild can't silently pick up a newer CLI with
# different flags/output shape than `runtime/claude.py` was written against --
# bump this deliberately (check `npm view @anthropic-ai/claude-code versions`)
# and re-test, never let it float. If your environment already has an internal
# mirror for this, point npm at it via a build arg instead.
# Node 22+ is required by the CLI (Debian's apt nodejs is 20), so copy the
# official Node 22 runtime + npm from the node:22-slim stage.
ARG CLAUDE_CODE_VERSION=2.1.283
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && claude --version
ENV FACTORY_CLAUDE_CLI_PATH=/usr/local/bin/claude

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

COPY factory-controller/deploy/entrypoint.sh /usr/local/bin/factory-entrypoint.sh
RUN chmod +x /usr/local/bin/factory-entrypoint.sh

RUN mkdir -p /var/lib/factory/sandboxes /var/lib/factory/repos /var/lib/factory/evidence \
    && chown -R factory:factory /var/lib/factory /app
USER factory

ENV FACTORY_MODE=production \
    FACTORY_SANDBOX_ROOT=/var/lib/factory/sandboxes \
    FACTORY_REPOS_ROOT=/var/lib/factory/repos \
    FACTORY_EVIDENCE_DIR=/var/lib/factory/evidence

EXPOSE 8080

# Builds FACTORY_DATABASE_URL from a mounted secret file when POSTGRES_PASSWORD_FILE
# is set (see entrypoint.sh, docker-compose.yml), so the DB password is never a
# plain env var visible to `docker inspect`/`compose config` (red-team #2 item 8).
ENTRYPOINT ["/usr/local/bin/factory-entrypoint.sh"]
# docker-compose.yml overrides this per service (`factory serve` / `factory worker`).
CMD ["factory", "serve", "--host", "0.0.0.0", "--port", "8080"]
