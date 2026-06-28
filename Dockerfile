FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_MAJOR=20 \
    BGUTIL_POT_HOME=/opt/bgutil-pot

# System deps: ffmpeg for video/audio merging+encoding, Node.js for bgutil-pot
# (the YouTube PO-token generator), git for cloning bgutil-pot.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        git \
        gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Build bgutil-pot (Node.js helper that mints YouTube Proof-of-Origin tokens —
# without it most YouTube videos return 403 / "format not available").
RUN git clone --depth=1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git ${BGUTIL_POT_HOME} \
    && cd ${BGUTIL_POT_HOME}/server \
    && npm ci \
    && npx tsc

# Install the bgutil-pot Python plugin into yt-dlp's plugin dir.
RUN mkdir -p /root/yt-dlp-plugins/bgutil-ytdlp-pot-provider \
    && cp -r ${BGUTIL_POT_HOME}/plugin/* /root/yt-dlp-plugins/bgutil-ytdlp-pot-provider/

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Long timeout because video downloads can take a while; single worker keeps
# the in-memory job dict consistent (jobs aren't shared across workers).
CMD ["sh", "-c", "gunicorn -w 1 -k gthread --threads 8 --timeout 900 -b 0.0.0.0:${PORT:-8000} app:app"]
