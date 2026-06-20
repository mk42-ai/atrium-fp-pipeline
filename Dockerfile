FROM python:3.11-slim

WORKDIR /usr/src/app

# Optional DWG->DXF ingest via LibreDWG (only used when base_file is a native .dwg;
# the pipeline falls back to a sibling .dxf if absent). Tolerate failure so the
# image still builds in minimal environments.
RUN (apt-get update \
     && apt-get install -y --no-install-recommends libredwg-tools \
     && rm -rf /var/lib/apt/lists/*) || true

# Headless matplotlib + writable HOME for its font cache.
ENV MPLBACKEND=Agg \
    HOME=/tmp \
    PORT=3000 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3000

CMD ["python", "server.py"]
