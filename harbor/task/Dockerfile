FROM ghcr.io/astral-sh/uv:0.12.24 AS uv
FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY harbor-constraints.txt /opt/harbor-constraints.txt
ENV UV_CONSTRAINT=/opt/harbor-constraints.txt

WORKDIR /opt/fixture
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --only-group fixture --no-install-project
COPY fixture_mcp.py fixture.json ./

WORKDIR /workspace
