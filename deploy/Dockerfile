# Octopus coordinator image. Build from the repository root, with submodules checked out:
#
#   git submodule update --init --recursive
#   docker build -f deploy/Dockerfile -t octopus-coordinator .
#
# Stage 1 builds liblogosdelivery (RLN is not used by Octopus, so no Rust toolchain).
# Stage 2 is a slim Python image with the library, the bindings and Octopus.

# BUILD liblogosdelivery ------------------------------------------------------
FROM debian:bookworm-slim AS lib

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git curl ca-certificates cmake jq libssl-dev libbsd-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY vendor/logos-delivery-python-bindings/vendor/logos-delivery ./logos-delivery
WORKDIR /src/logos-delivery

# Slowest step, kept apart so its layer is cached.
RUN make -j"$(nproc)" deps QUICK_AND_DIRTY_COMPILER=1
RUN make -j"$(nproc)" liblogosdelivery DISABLE_RLN=true

# RUNTIME ---------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS prod

LABEL org.opencontainers.image.source="https://github.com/logos-messaging/octopus"
LABEL org.opencontainers.image.description="Octopus coordinator"

RUN apt-get update && apt-get install -y --no-install-recommends \
      libssl3 libbsd0 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --home-dir /var/lib/octopus --shell /usr/sbin/nologin octopus

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY vendor/logos-delivery-python-bindings/logosdelivery ./bindings/logosdelivery
COPY --from=lib /src/logos-delivery/build/liblogosdelivery.so ./bindings/lib/liblogosdelivery.so

RUN pip install --no-cache-dir '.[logos]'

# Where octopus.transport.logos looks for the bindings (they load ./lib next to the package).
ENV OCTOPUS_BINDINGS=/app/bindings \
    OCTOPUS_DATA_DIR=/var/lib/octopus \
    OCTOPUS_WEBHOOK_LISTEN=0.0.0.0:8080 \
    OCTOPUS_METRICS_LISTEN=0.0.0.0:9100

# Node (TCP, and UDP for QUIC), webhook (behind a TLS proxy), ops (keep private).
EXPOSE 60000/tcp 60000/udp 8080 9100
VOLUME /var/lib/octopus
USER octopus

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/readyz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["octopus"]
CMD ["coordinator", "start"]
