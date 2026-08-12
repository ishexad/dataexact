"""
FastAPI router for the Excel Compare tool.

Flow:
1. POST /compare/upload   -> upload File A + File B, get back a pair_id
                              plus each file's sheet names
2. GET  /compare/columns   -> given pair_id + sheet_a + sheet_b, get back
                              columns common to both (candidates for the key)
3. POST /compare/generate  -> given pair_id + sheet_a + sheet_b + key_column,
                              get back the highlighted .xlsx diff report
"""

import os
import uuid

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse

import gating
from diff_engine import get_sheets, get_columns, build_diff_report

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

router = APIRouter(prefix="/compare")


def _path(pair_id: str, which: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{pair_id}_{which}.xlsx")


async def _save_upload(file: UploadFile, path: str):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, f"'{file.filename}' isn't a .xlsx/.xlsm file")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"'{file.filename}' is too large (10 MB limit)")
    with open(path, "wb") as f:
        f.write(contents)


@router.post("/upload")
async def upload(file_a: UploadFile = File(...), file_b: UploadFile = File(...)):
    pair_id = str(uuid.uuid4())
    path_a = _path(pair_id, "a")
    path_b = _path(pair_id, "b")

    await _save_upload(file_a, path_a)
    await _save_upload(file_b, path_b)

    try:
        sheets_a = get_sheets(path_a)
        sheets_b = get_sheets(path_b)
    except Exception:
        os.remove(path_a)
        os.remove(path_b)
        raise HTTPException(400, "Could not read one of the files as a valid Excel workbook")

    return {"pair_id": pair_id, "sheets_a": sheets_a, "sheets_b": sheets_b}


@router.get("/columns")
async def columns(pair_id: str, sheet_a: str, sheet_b: str):
    path_a = _path(pair_id, "a")
    path_b = _path(pair_id, "b")
    if not (os.path.exists(path_a) and os.path.exists(path_b)):
        raise HTTPException(404, "Unknown pair_id (may have expired) — please re-upload both files")

    try:
        cols_a = set(get_columns(path_a, sheet_a))
        cols_b = set(get_columns(path_b, sheet_b))
    except Exception:
        raise HTTPException(400, "Could not read the selected sheet in one of the files")

    common = [c for c in get_columns(path_a, sheet_a) if c in cols_b]
    only_a = sorted(cols_a - cols_b)
    only_b = sorted(cols_b - cols_a)

    return {"common_columns": common, "columns_only_in_a": only_a, "columns_only_in_b": only_b}


@router.post("/generate")
async def generate(
    request: Request,
    pair_id: str = Form(...),
    sheet_a: str = Form(...),
    sheet_b: str = Form(...),
    key_column: str = Form(...),
):
    logged_in = gating.gate(request, "compare")

    path_a = _path(pair_id, "a")
    path_b = _path(pair_id, "b")
    if not (os.path.exists(path_a) and os.path.exists(path_b)):
        raise HTTPException(404, "Unknown pair_id (may have expired) — please re-upload both files")

    output_id = str(uuid.uuid4())
    output_path = os.path.join(OUTPUT_DIR, f"{output_id}.xlsx")

    try:
        build_diff_report(path_a, sheet_a, path_b, sheet_b, key_column, output_path)
    except ValueError as e:
        raise HTTPException(400, str(e))

    gating.record_use(request, "compare", logged_in)

    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="comparison_report.xlsx",
    )
