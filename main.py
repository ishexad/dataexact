"""
FastAPI backend for the Excel -> Word report builder.

Flow:
1. POST /upload            -> upload .xlsx, get back a file_id + list of sheets
2. GET  /columns            -> given file_id + sheet, get back column names
3. POST /generate           -> given file_id + sheet + heading_column +
                                column_order (+ optional title), get back
                                the generated .docx file
"""

import os
import uuid
import time
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()  # local dev only — Render sets real env vars directly and has no .env file

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from converter import get_sheets, get_columns, build_report
import analytics
import anomaly_detector
import auth
import billing
import codebook
import compare
import format_report
import gating
import usage

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"

# Cleanup job settings: since this is a public tool, uploaded and generated
# files are temporary session artifacts, not something to retain. A sweep
# runs periodically and deletes anything older than FILE_MAX_AGE_SECONDS.
FILE_MAX_AGE_SECONDS = 60 * 60      # delete files older than 1 hour
CLEANUP_INTERVAL_SECONDS = 10 * 60  # run the sweep every 10 minutes

logger = logging.getLogger("uvicorn.error")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="Excel to Word Report Builder")

# Wide-open CORS for now since the frontend will likely be a separate
# static site talking to this API. Tighten to specific origin(s) at deploy time.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Needed for login: signs the session cookie that remembers who's logged in.
# SESSION_SECRET_KEY must be set as a real secret in production (Render env vars).
# Falls back to a dev-only key so this still runs locally without setup.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET_KEY", "dev-only-insecure-key-change-me"),
)

# dataexact.co.uk was the original domain; dataexact.io is now primary.
# Redirect any visitor/backlink on the old domain straight to the new one
# (same path/query) instead of running two live copies of the same site.
OLD_DOMAIN_HOSTS = {"dataexact.co.uk", "www.dataexact.co.uk"}
NEW_DOMAIN_HOST = "www.dataexact.io"


@app.middleware("http")
async def redirect_old_domain(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if host in OLD_DOMAIN_HOSTS:
        new_url = request.url.replace(scheme="https", netloc=NEW_DOMAIN_HOST)
        return RedirectResponse(url=str(new_url), status_code=301)
    return await call_next(request)


app.include_router(analytics.router)
app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(codebook.router)
app.include_router(compare.router)
app.include_router(format_report.router)
app.include_router(anomaly_detector.router)


def _max_age_for(directory: str, name: str) -> int:
    """Most artifacts are short-lived, but a Qualitative Coding corpus has to
    outlive the hour it takes to tag files and build a code tree — see
    codebook.CORPUS_MAX_AGE_SECONDS. Those are the *.json files in uploads/."""
    if directory == UPLOAD_DIR and name.endswith(".json"):
        return codebook.CORPUS_MAX_AGE_SECONDS
    return FILE_MAX_AGE_SECONDS


def _sweep_old_files():
    """Delete expired files from uploads/ and outputs/."""
    now = time.time()
    removed = 0
    for directory in (UPLOAD_DIR, OUTPUT_DIR):
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if os.path.isfile(path) and (now - os.path.getmtime(path)) > _max_age_for(directory, name):
                    os.remove(path)
                    removed += 1
            except FileNotFoundError:
                pass  # already removed by a concurrent sweep/request
    if removed:
        logger.info(f"cleanup: removed {removed} expired file(s)")


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            _sweep_old_files()
        except Exception as e:
            logger.warning(f"cleanup sweep failed: {e}")


@app.on_event("startup")
async def start_cleanup_job():
    _sweep_old_files()  # clear anything left over from a previous run first
    asyncio.create_task(_cleanup_loop())


def _file_path(file_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{file_id}.xlsx")


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload a .xlsx or .xlsm file")

    contents = await file.read()
    limit = gating.max_upload_bytes(request)
    if len(contents) > limit:
        analytics.log_event(request, "excel_to_word", analytics.EVENT_UPLOAD,
                            file_size_bytes=len(contents), success=False,
                            error_message=f"over the {limit // (1024 * 1024)} MB upload limit")
        raise HTTPException(400, f"File too large ({limit // (1024 * 1024)} MB limit)")

    file_id = str(uuid.uuid4())
    path = _file_path(file_id)
    with open(path, "wb") as f:
        f.write(contents)

    try:
        sheets = get_sheets(path)
    except Exception:
        os.remove(path)
        analytics.log_event(request, "excel_to_word", analytics.EVENT_UPLOAD,
                            file_size_bytes=len(contents), success=False,
                            error_message="not a valid Excel workbook")
        raise HTTPException(400, "Could not read this file as a valid Excel workbook")

    analytics.log_event(request, "excel_to_word", analytics.EVENT_UPLOAD,
                        file_size_bytes=len(contents))
    return {"file_id": file_id, "sheets": sheets}


@app.get("/columns")
async def columns(file_id: str, sheet: str):
    path = _file_path(file_id)
    if not os.path.exists(path):
        raise HTTPException(404, "Unknown file_id (may have expired) — please re-upload")
    try:
        cols = get_columns(path, sheet)
    except Exception:
        raise HTTPException(400, "Could not read that sheet")
    return {"columns": cols}


@app.get("/usage")
async def usage_status(request: Request, tool: str):
    if request.session.get("user"):
        return {"logged_in": True, "subscribed": billing.is_subscribed(request)}
    return {"logged_in": False, "remaining": usage.remaining(request, tool), "limit": usage.FREE_LIMIT}


@app.post("/generate")
async def generate(
    request: Request,
    file_id: str = Form(...),
    sheet: str = Form(...),
    heading_column: str = Form(...),
    column_order: str = Form(...),  # comma-separated, preserves user order
    title: str = Form(None),
):
    logged_in = gating.gate(request, "excel_to_word")

    path = _file_path(file_id)
    if not os.path.exists(path):
        raise HTTPException(404, "Unknown file_id (may have expired) — please re-upload")

    order = [c.strip() for c in column_order.split(",") if c.strip()]
    if not order:
        raise HTTPException(400, "column_order must contain at least one column")

    output_id = str(uuid.uuid4())
    output_path = os.path.join(OUTPUT_DIR, f"{output_id}.docx")

    try:
        build_report(path, sheet, heading_column, order, output_path, title=title)
    except Exception as e:
        analytics.log_event(request, "excel_to_word", analytics.EVENT_PROCESSED,
                            success=False, error_message=str(e))
        if isinstance(e, ValueError):
            raise HTTPException(400, str(e))
        raise

    analytics.log_event(request, "excel_to_word", analytics.EVENT_PROCESSED)
    gating.record_use(request, "excel_to_word", logged_in)

    analytics.log_event(request, "excel_to_word", analytics.EVENT_DOWNLOAD)
    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="report.docx",
    )


# StaticFiles mounts only match "/app/..." — this catches the bare "/app"
# (no trailing slash) that people will actually type/link and sends them
# to the mount.
@app.get("/app")
async def app_no_slash():
    return RedirectResponse(url="/app/")


# The tool's own UI lives at /app. Mounted before the marketing site so its
# prefix is matched first.
app.mount("/app", StaticFiles(directory="static/app", html=True), name="tool")

# Same pattern for the Compare Excel Files tool.
@app.get("/compare")
async def compare_no_slash():
    return RedirectResponse(url="/compare/")

app.mount("/compare", StaticFiles(directory="static/compare", html=True), name="compare-tool")

# Same pattern for the Format Report tool.
@app.get("/format")
async def format_no_slash():
    return RedirectResponse(url="/format/")

app.mount("/format", StaticFiles(directory="static/format", html=True), name="format-tool")

# Same pattern for the Detect Anomalies in Excel tool.
@app.get("/anomaly")
async def anomaly_no_slash():
    return RedirectResponse(url="/anomaly/")

app.mount("/anomaly", StaticFiles(directory="static/anomaly", html=True), name="anomaly-tool")

# Same pattern for the Qualitative Coding tool. Shares the "/codebook" prefix
# with codebook.router's API routes — exact API routes (POST /codebook/upload,
# /codebook/extract, /codebook/export) match before this Mount's catch-all,
# same as compare/format/anomaly above.
@app.get("/codebook")
async def codebook_no_slash():
    return RedirectResponse(url="/codebook/")

app.mount("/codebook", StaticFiles(directory="static/codebook", html=True), name="codebook-tool")

# DataExact marketing site at the root — its "Excel to Word" product card
# links to /app. Mounted last (root "/" would otherwise shadow everything).
app.mount("/", StaticFiles(directory="static/marketing", html=True), name="marketing")
