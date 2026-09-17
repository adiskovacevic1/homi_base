FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Enough of a system for the bot's `shell` tool to be useful out of the box.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini ca-certificates curl git jq less nano procps iproute2 iputils-ping \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
# A fresh install starts with these generic tools (copied into /data/tools on first start when it is empty) and grows from there.
COPY starter-tools/ /app/starter-tools/
# Runs as root: the bot's `install` tool needs apt, and it is alone in this container.
# pip --user lands in /data (the volume), so installs outlive a rebuild; apt is replayed at startup.
ENV PYTHONUSERBASE=/data/pylibs
RUN mkdir -p /data
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]
