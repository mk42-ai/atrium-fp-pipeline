#!/usr/bin/env python3
"""
atrium-fp-pipeline — serverless HTTP wrapper (On Demand serverless app).

Runs the self-contained fire_protection_pipeline IN-container (ezdxf +
matplotlib, headless Agg) and serves the generated artifacts over HTTP.
Native AutoCAD .dwg drawings are ingested via LibreDWG (dwg2dxf); .dxf is direct.

Routes:
  GET  /health                       -> service + engine status
  POST /run                          -> run the 6-stage pipeline; returns the
                                        3-gate verification verdict, device
                                        schedule, and downloadable artifact URLs
  GET  /artifact/<sheet>/<filename>  -> download a generated artifact
                                        (DXF / PDF / PNG / CSV / MD / JSON)

No API keys or credentials required — the pipeline is fully local.
"""
import base64
import os
import shutil
import subprocess
import traceback
from urllib.parse import urlparse, unquote
from urllib.request import urlopen, Request

from flask import Flask, request, jsonify, send_file, abort

from atrium_fp_pipeline import (
    fire_protection_pipeline,
    TOOL_ID,
    TOOL_NAME,
    TOOL_VERSION,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024  # 128 MB (base64 of a large drawing)

# Where pipeline runs write their artifacts (served back via /artifact).
OUT_ROOT = os.environ.get("OUT_ROOT", "/tmp/atrium_out")
# Public base used to build artifact URLs returned by /run.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://serverless.on-demand.io/apps/atrium-fp-pipeline"
).rstrip("/")

CONTENT_TYPES = {
    ".dxf": "application/dxf",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".json": "application/json",
}
# Formats that should download rather than render inline in a browser/agent.
ATTACH_EXTS = {".dxf", ".csv"}


def _safe_basename(url_or_name):
    name = os.path.basename(urlparse(url_or_name).path) or "base"
    name = unquote(name)
    if not os.path.splitext(name)[1]:
        name += ".dxf"  # pipeline ingest keys off the extension
    return name


def _download(url, dest_dir):
    fname = _safe_basename(url)
    dest = os.path.join(dest_dir, fname)
    req = Request(url, headers={"User-Agent": f"{TOOL_ID}/{TOOL_VERSION}"})
    with urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if os.path.getsize(dest) == 0:
        raise ValueError("downloaded base_file is empty")
    return dest


def _libredwg_version():
    """Return the LibreDWG version string, or None if dwg2dxf isn't installed."""
    exe = shutil.which("dwg2dxf")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        text = ((out.stdout or "") + " " + (out.stderr or "")).replace(",", " ")
        for tok in text.split():
            if tok[:1].isdigit() and "." in tok:
                return tok
        return "installed"
    except Exception:  # noqa: BLE001
        return "installed"


def _sanitize_dxf_handles(path):
    """Reassign invalid '0' object-handles (a known LibreDWG dwg2dxf artifact) so
    ezdxf can load the file. Returns the number of handles repaired."""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    except Exception:  # noqa: BLE001
        return 0
    existing = set()
    for i in range(len(lines) - 1):
        if lines[i].strip() in ("5", "105"):
            try:
                existing.add(int(lines[i + 1].strip(), 16))
            except ValueError:
                pass
    nxt = (max(existing) if existing else 0) + 1
    changed = 0
    for i in range(len(lines) - 1):
        if lines[i].strip() in ("5", "105") and lines[i + 1].strip() == "0":
            lines[i + 1] = format(nxt, "X")
            nxt += 1
            changed += 1
    if changed:
        open(path, "w", encoding="utf-8").write("\n".join(lines))
    return changed


def _prepare_base(local_path, workdir):
    """Normalise the base drawing to a DXF the pipeline can always read.

    Native AutoCAD .dwg is converted via LibreDWG (dwg2dxf); the output (and any
    .dxf input) then has invalid '0' handles repaired so ezdxf never chokes.
    Returns (dxf_path, ingest_note).
    """
    if not isinstance(local_path, str):
        raise RuntimeError("no base drawing provided")
    low = local_path.lower()
    if low.endswith(".dwg"):
        exe = shutil.which("dwg2dxf")
        if not exe:
            raise RuntimeError(
                "DWG input requires LibreDWG (dwg2dxf), which is not installed in this image"
            )
        raw = os.path.join(workdir, "_base_from_dwg.dxf")
        cp = subprocess.run(
            [exe, "-y", "-o", raw, local_path], capture_output=True, text=True, timeout=600
        )
        if not (os.path.exists(raw) and os.path.getsize(raw) > 1000):
            tail = ((cp.stderr or "") + (cp.stdout or ""))[-300:]
            raise RuntimeError(f"dwg2dxf could not convert the DWG: {tail}")
        fixed = _sanitize_dxf_handles(raw)
        return raw, f"converted DWG->DXF via LibreDWG (handles repaired: {fixed})"
    if low.endswith(".dxf"):
        fixed = _sanitize_dxf_handles(local_path)
        note = "ingested DXF directly" if not fixed else f"ingested DXF (handles repaired: {fixed})"
        return local_path, note
    return local_path, "ingested as-is"


@app.get("/health")
def health():
    import ezdxf
    import matplotlib

    return jsonify(
        {
            "status": "ok",
            "tool": TOOL_ID,
            "callable": TOOL_NAME,
            "version": TOOL_VERSION,
            "engine": {
                "ezdxf": ezdxf.__version__,
                "matplotlib": matplotlib.__version__,
            },
            "dwg2dxf": bool(shutil.which("dwg2dxf")),
            "libredwg": _libredwg_version(),
            "dwg_supported": bool(shutil.which("dwg2dxf")),
        }
    )


@app.post("/run")
def run():
    body = request.get_json(force=True, silent=True) or {}
    base_file = body.get("base_file")
    base_b64 = body.get("base_file_base64")
    sheet = body.get("floor_sheet_id")
    if not sheet or not (base_file or base_b64):
        return (
            jsonify(
                {
                    "error": "floor_sheet_id and one of base_file (URL to .dxf/.dwg) "
                    "or base_file_base64 are required"
                }
            ),
            400,
        )

    sheet = str(sheet)
    workdir = os.path.join(OUT_ROOT, sheet)
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)

    # Resolve the base drawing into workdir:
    #   base_file_base64 -> decode bytes; base_file URL -> download; else local path.
    try:
        if base_b64:
            if base_b64.lstrip().startswith("data:") and "," in base_b64:
                base_b64 = base_b64.split(",", 1)[1]
            data = base64.b64decode(base_b64)
            if len(data) < 64:
                raise ValueError("base_file_base64 decoded to too few bytes")
            local_base = os.path.join(
                workdir, _safe_basename(body.get("base_file_name") or "base.dxf")
            )
            with open(local_base, "wb") as f:
                f.write(data)
        elif isinstance(base_file, str) and base_file.lower().startswith(
            ("http://", "https://")
        ):
            local_base = _download(base_file, workdir)
        else:
            local_base = base_file
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"could not read base_file: {e}"}), 400

    # Convert DWG -> DXF (LibreDWG) and repair handles so ezdxf always loads it.
    try:
        local_base, ingest_note = _prepare_base(local_base, workdir)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"could not prepare base drawing: {e}"}), 400

    try:
        result = fire_protection_pipeline(
            base_file=local_base,
            floor_sheet_id=sheet,
            nfpa_params=body.get("nfpa_params") or {},
            ahj=body.get("ahj"),
            units=body.get("units"),
            scale=body.get("scale"),
            containment_tolerance_mm=body.get("containment_tolerance_mm"),
            output_formats=body.get("output_formats"),
            title_block=body.get("title_block"),
            workdir=workdir,
        )
    except Exception as e:  # noqa: BLE001
        return (
            jsonify(
                {
                    "error": "pipeline failed",
                    "details": str(e),
                    "trace": traceback.format_exc().splitlines()[-6:],
                }
            ),
            500,
        )

    # Rewrite local artifact paths -> public, downloadable URLs.
    artifacts = {}
    for key, path in (result.get("outputs") or {}).items():
        if path and os.path.isfile(path):
            artifacts[key] = f"{PUBLIC_BASE_URL}/artifact/{sheet}/{os.path.basename(path)}"
    result["artifacts"] = artifacts
    if isinstance(result.get("snapshot"), dict):
        result["snapshot"]["ingest"] = ingest_note
    result.pop("outputs", None)  # drop absolute container paths from the response
    result.pop("log", None)
    return jsonify(result)


@app.get("/artifact/<sheet>/<path:filename>")
def artifact(sheet, filename):
    safe = os.path.normpath(filename)
    if safe.startswith("..") or os.path.isabs(safe):
        abort(400)
    path = os.path.join(OUT_ROOT, sheet, safe)
    if not os.path.isfile(path):
        abort(404)
    ext = os.path.splitext(path)[1].lower()
    return send_file(
        path,
        mimetype=CONTENT_TYPES.get(ext, "application/octet-stream"),
        as_attachment=ext in ATTACH_EXTS,
        download_name=os.path.basename(path),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
