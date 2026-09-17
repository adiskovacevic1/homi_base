# Dev box: a small Debian with Python 3.12, Node 22, git and Claude Code.
# You shell into this to build bots; each finished bot gets its own container (see bots/example-bot).
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client jq less nano procps sqlite3 tini \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Common bot libraries preinstalled so a new bot can start immediately.
RUN pip install anthropic discord.py aiohttp python-dotenv httpx cryptography

WORKDIR /bots
ENTRYPOINT ["/usr/bin/tini", "--"]
# The forge: builds tools for the bots with a headless Claude Code (see forge.py). No INTERNAL_TOKEN -> it idles.
COPY dev/forge.py dev/setup.py dev/setup_web.py dev/setup.html bots/example-bot/vault.py /opt/forge/
COPY dev/assets/ /opt/forge/assets/
CMD ["python", "/opt/forge/forge.py"]
