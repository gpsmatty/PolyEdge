FROM python:3.13-slim

WORKDIR /app

# System deps for web3/eth-account (needs build tools for some wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy source and config
COPY pyproject.toml .
COPY src/ src/
COPY config/ config/

# Install package and all dependencies
RUN pip install --no-cache-dir .

# Unbuffered stdout/stderr — critical for real-time logs in DO
ENV PYTHONUNBUFFERED=1

# Launcher: health server (port 8080) + strategy as a controllable task.
# Control endpoints: /status, /stop, /start — so you can pause trading
# without killing the container.
# Override strategy via DO app spec or "Run Command" in Settings.
CMD ["polyedge", "run", "--strategy", "micro", "--market", "btc 15m"]
