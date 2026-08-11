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
import auth
import billing
import compare
import usage

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB cap for a public tool

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

app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(compare.router)


def _sweep_old_files():
    """Delete files older than FILE_MAX_AGE_SECONDS from uploads/ and outputs/."""
    now = time.time()
    removed = 0
    for directory in (UPLOAD_DIR, OUTPUT_DIR):
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if os.path.isfile(path) and (now - os.path.getmtime(path)) > FILE_MAX_AGE_SECONDS:
                    os.remove(path)
                    removed += 1
            except FileNotFoundError:
                pass  # already removed by a concurrent sweep/request
    if removed:
        logger.info(f"cleanup: removed {removed} file(s) older than {FILE_MAX_AGE_SECONDS}s")


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
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload a .xlsx or .xlsm file")

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (10 MB limit)")

    file_id = str(uuid.uuid4())
    path = _file_path(file_id)
    with open(path, "wb") as f:
        f.write(contents)

    try:
        sheets = get_sheets(path)
    except Exception:
        os.remove(path)
        raise HTTPException(400, "Could not read this file as a valid Excel workbook")

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
async def usage_status(request: Request):
    if request.session.get("user"):
        return {"logged_in": True, "subscribed": billing.is_subscribed(request)}
    return {"logged_in": False, "remaining": usage.remaining(request), "limit": usage.FREE_LIMIT}


@app.post("/generate")
async def generate(
    request: Request,
    file_id: str = Form(...),
    sheet: str = Form(...),
    heading_column: str = Form(...),
    column_order: str = Form(...),  # comma-separated, preserves user order
    title: str = Form(None),
):
    logged_in = bool(request.session.get("user"))
    if not logged_in:
        usage.enforce_limit(request)
    elif not billing.is_subscribed(request):
        raise HTTPException(402, {
            "reason": "subscription_required",
            "message": "Subscribe to keep generating reports.",
        })

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
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not logged_in:
        usage.record_use(request)

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

# DataExact marketing site at the root — its "Excel to Word" product card
# links to /app. Mounted last (root "/" would otherwise shadow everything).
app.mount("/", StaticFiles(directory="static/marketing", html=True), name="marketing")
