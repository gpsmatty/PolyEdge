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

# Starts only the health-check server so DO passes readiness probes.
# Start trading strategies manually from the DO console:
#   polyedge micro --auto --market "btc 15m"
#   polyedge sniper --auto
#   polyedge weather --auto
CMD ["polyedge", "health-server"]
