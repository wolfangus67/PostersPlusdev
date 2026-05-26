# ── Builder stage ─────────────────────────────────────────────────────────────
# Compiles pycairo (and any future C-extension wheels) against the cairo dev
# headers, then we copy only the resulting wheels into the runtime image so the
# ~200MB of build toolchain doesn't ship to users.
FROM python:3.11-slim AS builder
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libcairo2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels --no-cache-dir -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app

# libcairo2 (runtime only — no -dev headers needed) for pycairo;
# gosu for privilege drop in entrypoint.sh.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

RUN adduser --disabled-password --gecos '' appuser

# Copy app files and set ownership on everything except the cache dir,
# which is a runtime volume mount — permissions are fixed by entrypoint.sh.
COPY . .
RUN chown -R appuser:appuser /app

# Run as root so entrypoint.sh can fix cache volume permissions at startup,
# then it drops to appuser via gosu before exec-ing uvicorn.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)" || exit 1
CMD ["/bin/sh", "entrypoint.sh"]
