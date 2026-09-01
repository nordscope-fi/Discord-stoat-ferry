FROM python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1 AS builder

ENV UV_PROJECT_ENVIRONMENT=/opt/feedback-venv \
    UV_LINK_MODE=copy

WORKDIR /build

COPY services/feedback/uv-requirements.txt ./uv-requirements.txt
RUN python -m pip install --require-hashes --no-cache-dir -r uv-requirements.txt

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable --extra feedback-service


FROM python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1 AS runtime

ENV PATH=/opt/feedback-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 feedback \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin feedback \
    && install -d -o 10001 -g 10001 -m 0700 /data

COPY --from=builder --chown=10001:10001 /opt/feedback-venv /opt/feedback-venv

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()"]

CMD ["python", "-m", "discord_ferry.feedback_service", "serve", "--host", "0.0.0.0", "--port", "8080"]
