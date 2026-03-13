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

# Launcher: health server (port 8080) + controllable strategy.
# Starts paused — no trading until you tell it what to run:
#   curl "localhost:8080/start?strategy=micro&market=btc+15m"
#   curl "localhost:8080/start?strategy=sniper"
#   curl "localhost:8080/start?strategy=weather"
# Or override CMD in DO app spec to auto-start a specific strategy:
#   polyedge run -s micro -m "btc 15m"
CMD ["polyedge", "run", "--paused"]
