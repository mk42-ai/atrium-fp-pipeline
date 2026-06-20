# atrium-fp-pipeline (On Demand serverless app)

Reusable Atrium Tower fire-protection CAD pipeline — `ezdxf 1.4.4` + `matplotlib`,
wrapped as a self-contained HTTP service for On Demand serverless. Pure-local: no
API keys, no network backend required.

Given a base architectural drawing (DXF preferred, or DWG) and a floor sheet ID, it
produces an architecture-preserving NFPA fire-protection overlay, runs a 3-gate
self-verification, and exports DXF + PDF + PNG with a per-device traceability CSV.

## Endpoints

| Method | Route | Purpose |
|--------|-------|---------|
| `GET`  | `/health` | Service + engine status |
| `POST` | `/run` | Run the 6-stage pipeline; returns verification + artifact URLs |
| `GET`  | `/artifact/<sheet>/<filename>` | Download a generated artifact |

### `POST /run`

```json
{
  "base_file": "https://example.com/A-111_Tower-A_Typical_Floor.dxf",
  "floor_sheet_id": "A-111",
  "ahj": "ADCD",
  "units": "mm",
  "scale": "1:200 (at A1)",
  "containment_tolerance_mm": 8000,
  "output_formats": ["dxf", "pdf", "png"],
  "nfpa_params": {},
  "title_block": { "revision": "FP-Final" }
}
```

Returns the 3-gate verification verdict, the auto device schedule, the device total,
and `artifacts` URLs for the seven outputs (DXF, PDF, PNG, CSV, verification MD/JSON,
base snapshot JSON).

## Layout

```
Dockerfile             multi-stage: builds LibreDWG (dwg2dxf) from source + python runtime (gunicorn)
requirements.txt       ezdxf==1.4.2, matplotlib==3.9.4, Flask, gunicorn
server.py              Flask app (/health, /run, /artifact) served by gunicorn
openapi-schema.json    OpenAPI 3.0 spec (used by the On Demand plugin)
atrium_fp_pipeline/    the pipeline package (fire_protection_pipeline)
```

**Native DWG ingest:** AutoCAD `.dwg` drawings are converted in-container via
LibreDWG (`dwg2dxf`), compiled from source in the build stage (it is not in
Debian apt). `.dxf` is ingested directly. `base_file` accepts a URL; or pass the
drawing bytes as `base_file_base64` (+ `base_file_name`). `/health` reports the
LibreDWG version under `libredwg`.

## Run locally

```bash
pip install -r requirements.txt
python server.py            # listens on :3000 (or $PORT)
curl localhost:3000/health
```

Container runtime sets `MPLBACKEND=Agg` and `HOME=/tmp` for headless rendering.
