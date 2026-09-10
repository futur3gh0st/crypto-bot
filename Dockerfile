# Headless desk for a VPS near the venue. See DEPLOY.md for why us-east-2.
FROM python:3.13-slim

# tini reaps zombies and forwards SIGTERM, so `docker stop` reaches the desk's
# own signal handler and it shuts sleeves down cleanly instead of being killed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not reinstall the world.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps -e .

COPY config.yaml ./

# Paper sessions and ledgers live here. Mount a volume or the P&L resets to
# $1,000 on every redeploy and the calibration report loses its history.
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
# 0.0.0.0 inside the container is the container's own network namespace, not the
# host's; publish it with `-p 127.0.0.1:8787:8787` and it stays on loopback.
CMD ["stablebot", "desk", "--headless", "--serve", "0.0.0.0:8787"]
