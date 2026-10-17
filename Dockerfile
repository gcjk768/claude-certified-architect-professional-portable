# CCAR-P quiz bot: Telegram + Claude Code (claude -p) for the tutor explanations.
FROM python:3.12-slim
ARG CLAUDE_CODE_VERSION=
ARG PUID=1000
ARG PGID=1000
ENV TZ=Asia/Singapore DISABLE_AUTOUPDATER=1 PYTHONUNBUFFERED=1 \
    PATH=/home/quiz/.local/bin:$PATH DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates bash tzdata graphviz \
    && rm -rf /var/lib/apt/lists/*
# The gid may already exist in the base image (10 = uucp on Debian): reuse it rather than fail.
RUN (getent group "$PGID" >/dev/null || groupadd -g "$PGID" quiz) \
    && useradd -m -u "$PUID" -g "$PGID" -s /bin/bash quiz
USER quiz
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && claude --version
USER root
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py scrape.py online_parsers.py ./
COPY assets ./assets
# The build folder on the NAS is mode 770, and COPY keeps the mode but makes the files root-owned: the bot user could not read them.
RUN chmod -R a+rX /app
RUN mkdir -p /data && chown "$PUID:$PGID" /data
USER quiz
CMD ["python", "bot.py"]
