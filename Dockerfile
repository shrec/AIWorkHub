# Container image for the AIWorkHub stdio MCP server: `docker run -i --rm <image>`.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/aiworkhub
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY src ./src
COPY tests/support/__init__.py tests/support/retention.py ./tests/support/
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 --user-group --shell /usr/sbin/nologin mcp \
    && mkdir /workspace \
    && chown mcp:mcp /workspace
USER mcp
RUN git init --quiet /workspace/repo
WORKDIR /workspace/repo

ENV AIWORKHUB_REPO_ROOT=/workspace/repo AIWORKHUB_ALLOW_WRITES=0 AIWORKHUB_ALLOW_LAUNCH=0 PYTHONUNBUFFERED=1

LABEL io.modelcontextprotocol.server.name="io.github.shrec/aiworkhub"
LABEL org.opencontainers.image.source="https://github.com/shrec/AIWorkHub"
LABEL org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["aiworkhub-mcp"]
