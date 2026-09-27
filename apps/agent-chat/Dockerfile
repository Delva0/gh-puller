# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS web
WORKDIR /build
COPY apps/agent-chat/web/package.json apps/agent-chat/web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY apps/agent-chat/web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /source
COPY pyproject.toml README.md ./
COPY apps/agent-chat/pyproject.toml apps/agent-chat/uv.lock ./apps/agent-chat/
WORKDIR /source/apps/agent-chat
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project --no-install-package gh-puller
COPY gh_puller /source/gh_puller
COPY apps/agent-chat/agent_chat ./agent_chat
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    --reinstall-package gh-puller --reinstall-package gh-puller-agent-chat

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS runtime
RUN useradd --create-home --uid 10001 chat
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=dependencies /opt/venv /opt/venv
WORKDIR /app
COPY --from=web /build/dist ./web/dist
COPY apps/agent-chat/start.sh ./start.sh
ARG APP_REVISION=development
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    CHAT_STATIC_DIR=/app/web/dist CHAT_SECURE_COOKIE=true PORT=10000 APP_REVISION=$APP_REVISION
LABEL org.opencontainers.image.source="https://github.com/Delva0/gh-puller" \
      org.opencontainers.image.revision=$APP_REVISION
USER chat
EXPOSE 10000
CMD ["sh", "/app/start.sh"]
