# syntax=docker/dockerfile:1
# =============================================================================
# atrium-fp-pipeline — serverless container
# Stage 1 compiles LibreDWG (dwg2dxf) from source — it is NOT in Debian apt —
# so native AutoCAD .dwg drawings can be ingested. Stage 2 is a lean python
# runtime that copies only the built binary + library.
# =============================================================================

# ---- Stage 1: build LibreDWG `dwg2dxf` (native DWG -> DXF) ----
FROM python:3.11-slim-bookworm AS libredwg
ARG LIBREDWG_VERSION=0.13.4
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential wget ca-certificates xz-utils perl pkg-config texinfo; \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN set -eux; \
    wget -q "https://github.com/LibreDWG/libredwg/releases/download/${LIBREDWG_VERSION}/libredwg-${LIBREDWG_VERSION}.tar.xz"; \
    tar -xf "libredwg-${LIBREDWG_VERSION}.tar.xz"; \
    cd "libredwg-${LIBREDWG_VERSION}"; \
    ./configure --prefix=/opt/libredwg \
        --disable-bindings --disable-static --disable-dependency-tracking; \
    make -j"$(nproc)"; \
    make install; \
    /opt/libredwg/bin/dwg2dxf --version

# ---- Stage 2: runtime ----
FROM python:3.11-slim-bookworm

# Bring in the compiled LibreDWG tools (dwg2dxf, dxf2dwg, dwgread, ...).
COPY --from=libredwg /opt/libredwg /opt/libredwg

# PATH so the pipeline's shutil.which("dwg2dxf") finds it; LD_LIBRARY_PATH so the
# binary loads libredwg.so. MPLBACKEND/HOME for headless matplotlib rendering.
ENV PATH="/opt/libredwg/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/libredwg/lib" \
    MPLBACKEND=Agg \
    HOME=/tmp \
    PORT=3000 \
    PYTHONUNBUFFERED=1

# DejaVu fonts for crisp matplotlib text; CA certs for HTTPS base_file fetches.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends fonts-dejavu-core ca-certificates; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 3000

# Fail the build loudly if DWG support didn't make it into the runtime image.
RUN dwg2dxf --version

# Production WSGI server: 2 workers (Pro = 2 vCPU) x threads, long timeout for the
# 300-DPI matplotlib render of large drawings. Honour an injected $PORT, else 3000.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-3000} --workers 2 --threads 4 --timeout 600 --graceful-timeout 600 --access-logfile - --error-logfile - server:app"]
