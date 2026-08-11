"""
FastAPI router + detection engine for the Detect Anomalies in Excel tool.

Flow:
1. POST /anomaly/upload  -> upload .xlsx, get back a file_id + list of sheets
2. POST /anomaly/detect  -> given file_id + sheet, get back a JSON list of
                              everything that looks wrong in that sheet

Design: four independent detector functions, each scanning the whole sheet
for one category of issue and returning a flat list of
{row, column, type, detail} dicts (column is None for whole-row issues like
duplicates). `detect()` runs all four and merges the results. Every
detector is deliberately conservative — thresholds are tuned to flag things
that are a minority within their own column, so a column that's
consistently symbol-heavy or mixed-type isn't flagged just for being
itself; only the outliers *within* that column's own pattern are.
"""

import os
import re
import uuid
from collections import Counter

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

import gating

UPLOAD_DIR = "uploads"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB, same cap as the other tools

os.makedirs(UPLOAD_DIR, exist_ok=True)

router = APIRouter(prefix="/anomaly")

_SYMBOL_RE = re.compile(r"[^\w\s.,\-@()/']")
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_ID_HINTS = ("id", "email", "sku", "code", "key", "username")

_DATE_PATTERNS = [
    ("YYYY-MM-DD", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("MM/DD/YYYY", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("DD-MM-YYYY", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("Month DD, YYYY", re.compile(r"^[A-Za-z]+ \d{1,2},? \d{4}$")),
]


def get_sheets(excel_path: str) -> list[str]:
    return pd.ExcelFile(excel_path).sheet_names


def _excel_row(pandas_index: int) -> int:
    """Row number as the user would see it in Excel (header is row 1)."""
    return pandas_index + 2


def _case_style(s: str) -> str:
    if not any(c.isalpha() for c in s):
        return "none"
    if s == s.lower():
        return "lower"
    if s == s.upper():
        return "upper"
    if s == s.title():
        return "title"
    return "mixed"


def _classify_date_pattern(s: str):
    for name, pattern in _DATE_PATTERNS:
        if pattern.match(s):
            return name
    return None


def _find_duplicate_rows(df: pd.DataFrame) -> list[dict]:
    issues = []

    def row_key(idx, normalize):
        vals = []
        for v in df.loc[idx]:
            if pd.isna(v):
                vals.append(None)
            elif isinstance(v, str) and normalize:
                vals.append(v.strip().lower())
            else:
                vals.append(v)
        return tuple(vals)

    def group_and_flag(normalize, exclude, type_name, note):
        groups = {}
        for idx in df.index:
            if idx in exclude:
                continue
            groups.setdefault(row_key(idx, normalize), []).append(idx)
        flagged = set()
        for idxs in groups.values():
            if len(idxs) > 1:
                first = idxs[0]
                for idx in idxs[1:]:
                    issues.append({
                        "row": _excel_row(idx),
                        "column": None,
                        "type": type_name,
                        "detail": f"{note} row {_excel_row(first)}",
                    })
                flagged.update(idxs)
        return flagged

    exact_flagged = group_and_flag(False, set(), "Duplicate row", "Identical to")
    group_and_flag(True, exact_flagged, "Near-duplicate row", "Matches (aside from spacing/capitalization)")

    return issues


def _find_formatting_issues(df: pd.DataFrame) -> list[dict]:
    issues = []
    for col in df.columns:
        series = df[col]
        # Don't gate on dtype == object: pandas 2.x/3.x may store text columns
        # as a dedicated StringDtype instead of legacy object. Just look at
        # the actual values.
        str_vals = {idx: v for idx, v in series.dropna().items() if isinstance(v, str)}
        if not str_vals:
            continue

        for idx, v in str_vals.items():
            stripped = v.strip()
            if v != stripped:
                issues.append({
                    "row": _excel_row(idx), "column": str(col),
                    "type": "Extra whitespace",
                    "detail": f"Leading/trailing whitespace: {v!r}",
                })
            elif "  " in v:
                issues.append({
                    "row": _excel_row(idx), "column": str(col),
                    "type": "Extra whitespace",
                    "detail": f"Multiple consecutive spaces: {v!r}",
                })

        symbol_hits = {idx: v for idx, v in str_vals.items() if _SYMBOL_RE.search(v)}
        if symbol_hits and len(symbol_hits) / len(str_vals) < 0.15:
            for idx, v in symbol_hits.items():
                found = "".join(sorted(set(_SYMBOL_RE.findall(v))))
                issues.append({
                    "row": _excel_row(idx), "column": str(col),
                    "type": "Unusual character(s)",
                    "detail": f"{v!r} has characters not seen elsewhere in this column: {found}",
                })

        styles = {idx: _case_style(v) for idx, v in str_vals.items() if _case_style(v) != "none"}
        if styles:
            dominant_style, dominant_count = Counter(styles.values()).most_common(1)[0]
            if dominant_count / len(styles) > 0.7:
                for idx, style in styles.items():
                    if style != dominant_style:
                        issues.append({
                            "row": _excel_row(idx), "column": str(col),
                            "type": "Inconsistent capitalization",
                            "detail": f"{str_vals[idx]!r} doesn't match this column's usual {dominant_style} case",
                        })

        date_hits = {idx: _classify_date_pattern(v.strip()) for idx, v in str_vals.items()}
        date_hits = {idx: p for idx, p in date_hits.items() if p}
        if len(date_hits) >= 3:
            dominant_pattern, dominant_count = Counter(date_hits.values()).most_common(1)[0]
            if dominant_count / len(date_hits) > 0.5:
                for idx, pattern_name in date_hits.items():
                    if pattern_name != dominant_pattern:
                        issues.append({
                            "row": _excel_row(idx), "column": str(col),
                            "type": "Inconsistent date format",
                            "detail": f"{str_vals[idx]!r} looks like {pattern_name}, but this column is usually {dominant_pattern}",
                        })
    return issues


def _find_missing_and_type_issues(df: pd.DataFrame) -> list[dict]:
    issues = []
    n = len(df)
    if n == 0:
        return issues

    for col in df.columns:
        series = df[col]
        non_null = series.dropna()
        non_null_ratio = len(non_null) / n

        if 0 < non_null_ratio < 1 and non_null_ratio >= 0.7:
            for idx in series.index[series.isna()]:
                issues.append({
                    "row": _excel_row(idx), "column": str(col),
                    "type": "Missing value",
                    "detail": f"'{col}' is filled in {non_null_ratio:.0%} of rows, but this cell is empty",
                })

        if not pd.api.types.is_numeric_dtype(series) and len(non_null) >= 5:
            numeric_idx, text_idx = [], []
            for idx, v in non_null.items():
                (numeric_idx if _NUMERIC_RE.match(str(v).strip()) else text_idx).append(idx)
            total = len(numeric_idx) + len(text_idx)
            numeric_ratio = len(numeric_idx) / total
            if numeric_ratio >= 0.8 and text_idx:
                for idx in text_idx:
                    issues.append({
                        "row": _excel_row(idx), "column": str(col),
                        "type": "Unexpected type for this column",
                        "detail": f"'{col}' is mostly numeric, but this value is text: {non_null[idx]!r}",
                    })
            elif (1 - numeric_ratio) >= 0.8 and numeric_idx:
                for idx in numeric_idx:
                    issues.append({
                        "row": _excel_row(idx), "column": str(col),
                        "type": "Unexpected type for this column",
                        "detail": f"'{col}' is mostly text, but this value looks numeric: {non_null[idx]!r}",
                    })
    return issues


def _looks_like_id_column(col_name, series: pd.Series) -> bool:
    if any(hint in str(col_name).lower() for hint in _ID_HINTS):
        return True
    non_null = series.dropna()
    return len(non_null) >= 5 and non_null.nunique() / len(non_null) > 0.95


def _find_outliers_and_duplicate_ids(df: pd.DataFrame) -> list[dict]:
    issues = []
    for col in df.columns:
        series = df[col]
        orig_non_null = series.dropna()

        numeric_non_null = pd.to_numeric(series, errors="coerce").dropna()
        if len(orig_non_null) > 0 and len(numeric_non_null) >= 5 \
                and len(numeric_non_null) / len(orig_non_null) >= 0.8:
            q1, q3 = numeric_non_null.quantile(0.25), numeric_non_null.quantile(0.75)
            iqr = q3 - q1
            if iqr > 0:
                low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                for idx, v in numeric_non_null.items():
                    if v < low or v > high:
                        issues.append({
                            "row": _excel_row(idx), "column": str(col),
                            "type": "Outlier value",
                            "detail": f"{v:g} is far outside the typical range for '{col}' ({low:.2f} to {high:.2f})",
                        })

        if _looks_like_id_column(col, series):
            seen = {}
            for idx, v in orig_non_null.items():
                key = str(v).strip().lower()
                if key in seen:
                    issues.append({
                        "row": _excel_row(idx), "column": str(col),
                        "type": "Duplicate value in a likely-unique column",
                        "detail": f"{v!r} also appears in row {_excel_row(seen[key])}",
                    })
                else:
                    seen[key] = idx
    return issues


def detect(excel_path: str, sheet_name) -> dict:
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    if df.empty:
        raise ValueError("This sheet has no data rows to check")

    issues = (
        _find_duplicate_rows(df)
        + _find_formatting_issues(df)
        + _find_missing_and_type_issues(df)
        + _find_outliers_and_duplicate_ids(df)
    )
    issues.sort(key=lambda i: (i["row"], i["column"] or ""))

    return {
        "row_count": len(df),
        "column_count": len(df.columns),
        "issues": issues,
        "summary": dict(Counter(i["type"] for i in issues)),
    }


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload a .xlsx or .xlsm file")

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (10 MB limit)")

    file_id = str(uuid.uuid4())
    path = os.path.join(UPLOAD_DIR, f"{file_id}.xlsx")
    with open(path, "wb") as f:
        f.write(contents)

    try:
        sheets = get_sheets(path)
    except Exception:
        os.remove(path)
        raise HTTPException(400, "Could not read this file as a valid Excel workbook")

    return {"file_id": file_id, "sheets": sheets}


@router.post("/detect")
async def detect_endpoint(
    request: Request,
    file_id: str = Form(...),
    sheet: str = Form(...),
):
    logged_in = gating.gate(request)

    path = os.path.join(UPLOAD_DIR, f"{file_id}.xlsx")
    if not os.path.exists(path):
        raise HTTPException(404, "Unknown file_id (may have expired) — please re-upload")

    try:
        result = detect(path, sheet)
    except ValueError as e:
        raise HTTPException(400, str(e))

    gating.record_use(request, logged_in)
    return result
