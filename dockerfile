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

# Bake the EAST text-detection model into the image.  TEXTLESS_TEXT_DETECTION is
# on by default, so baking avoids the one-time ~96MB runtime download that would
# otherwise stall the first low-vote textless request, and it survives cache-
# volume wipes / works on air-gapped hosts.  Adds ~96MB to the image, and makes
# the build depend on EAST_MODEL_URL being reachable.  Opt out for a lean image
# (e.g. if you disable detection) — the model then downloads once at runtime:
#   docker build --build-arg BAKE_EAST_MODEL=false ...
#   (or set BAKE_EAST_MODEL=false in .env when building via compose)
ARG BAKE_EAST_MODEL=true
ARG EAST_MODEL_URL=https://github.com/oyyd/frozen_east_text_detection.pb/raw/master/frozen_east_text_detection.pb
RUN if [ "$BAKE_EAST_MODEL" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl && \
      mkdir -p /app/models && \
      curl -fsSL "$EAST_MODEL_URL" -o /app/models/frozen_east_text_detection.pb && \
      apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* ; \
    fi

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
