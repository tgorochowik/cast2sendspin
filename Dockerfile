# syntax=docker/dockerfile:1

# npm and build-time tooling do not enter the runtime image.
FROM node:26-trixie-slim AS node-deps
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM python:3.14-slim-trixie AS python-deps
COPY requirements.txt /build/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: \
       -r /build/requirements.txt

FROM python:3.14-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    NODE_USE_SYSTEM_CA=1 \
    MA_TOKEN_FILE=/run/secrets/ma_token \
    MANAGER_LOG_FILE=/app/.state/manager.log \
    LEGACY_BRIDGE_LOG=/app/.state/bridge.log
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /tmp app
WORKDIR /app
COPY --from=node-deps /usr/local/bin/node /usr/local/bin/node
COPY --from=node-deps /build/node_modules ./node_modules
COPY --from=python-deps /opt/venv /opt/venv
RUN ln -s /opt/venv /app/.venv \
    && mkdir -p /app/.state \
    && chown app:app /app/.state
COPY package.json ./
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src/ ./src/
USER 1000:1000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["node", "src/healthcheck.mjs"]
CMD ["node", "src/manager.mjs"]

# Optional offline unit-test target: docker build --target test .
FROM runtime AS test
COPY Dockerfile .dockerignore .gitignore package-lock.json requirements.txt ./
COPY tests/ ./tests/
RUN node --test tests/*.test.mjs \
    && .venv/bin/python -m unittest discover -s tests

FROM runtime AS final
