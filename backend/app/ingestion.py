"""Upload -> validated DataFrame -> Parquet -> profile. Replaces the old services.py."""
import re
import warnings
import zipfile
from pathlib import Path

import pandas as pd

from . import config
from .utils import json_safe

ALLOWED_EXTENSIONS = config.ALLOWED_EXTENSIONS
MAX_UPLOAD_BYTES = config.MAX_UPLOAD_MB * 1024 * 1024


class IngestionError(ValueError):
    pass


_CONTROL = re.compile(r"[\x00-\x1f\x7f<>`]")


def clean_column_name(name, position: int) -> str:
    """Column names reach prompts and SQL identifiers, so strip control chars and brackets."""
    text = _CONTROL.sub(" ", str(name)).strip()
    text = re.sub(r"\s+", " ", text)[:64]
    return text or f"column_{position + 1}"


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        key = n.lower()
        if key in seen:
            seen[key] += 1
            out.append(f"{n}_{seen[key]}")
        else:
            seen[key] = 0
            out.append(n)
    return out


def _check_file_kind(path: Path, ext: str) -> None:
    """Cheap content checks so a renamed binary never reaches pandas/openpyxl."""
    with path.open("rb") as fh:
        head = fh.read(8192)
    if ext == ".csv":
        if b"\x00" in head or head.startswith(b"PK\x03\x04"):
            raise IngestionError("File does not look like a text CSV")
        return
    if not head.startswith(b"PK\x03\x04"):
        raise IngestionError("File is not a valid .xlsx workbook")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            unzipped = sum(i.file_size for i in infos)
    except zipfile.BadZipFile:
        raise IngestionError("File is not a valid .xlsx workbook")
    # a small zip can expand to many GB: refuse before openpyxl parses anything
    if len(infos) > 5000 or unzipped > config.MAX_UNZIPPED_MB * 1024 * 1024:
        raise IngestionError("Workbook is too large once unpacked")


def _check_width(n_columns: int) -> None:
    if n_columns > config.MAX_COLUMNS:
        raise IngestionError(f"Too many columns (max {config.MAX_COLUMNS})")


def _read_csv(path: Path) -> pd.DataFrame:
    limit = config.MAX_ROWS + 1  # one extra row tells us the file is over the limit
    kwargs = dict(encoding="utf-8", encoding_errors="replace", low_memory=False, nrows=limit)
    # read only the header first, so a one-line file with a million columns cannot exhaust memory
    first_line = path.read_text(encoding="utf-8", errors="replace")[:65536].splitlines()
    first_line = first_line[0] if first_line else ""
    counts = {c: first_line.count(c) for c in (",", ";", "\t", "|")}
    best = max(counts, key=counts.get)  # the delimiter that appears most in the header line
    if counts[best] and best != ",":
        kwargs["sep"] = best
    _check_width(pd.read_csv(path, **{**kwargs, "nrows": 0}).shape[1])
    return pd.read_csv(path, **kwargs)


def read_table(path: Path, ext: str) -> pd.DataFrame:
    _check_file_kind(path, ext)
    try:
        if ext == ".csv":
            df = _read_csv(path)
        else:
            # pandas reads cached cell values, never formulas, so no formula can execute here
            df = pd.read_excel(path, engine="openpyxl", nrows=config.MAX_ROWS + 1)
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError(f"Could not read file as {ext[1:].upper()}: {type(exc).__name__}")
    if df.shape[1] == 0 or df.shape[0] == 0:
        raise IngestionError("File has no data rows or no columns")
    _check_width(df.shape[1])
    if df.shape[0] > config.MAX_ROWS:
        raise IngestionError(f"Too many rows (max {config.MAX_ROWS:,})")
    df.columns = _dedupe([clean_column_name(c, i) for i, c in enumerate(df.columns)])
    return df


def _try_datetime(series: pd.Series) -> pd.Series | None:
    if not (series.dtype == object or str(series.dtype) == "string"):
        return None
    sample = series.dropna().astype(str).head(200)
    if sample.empty or not sample.str.contains(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}").mean() > 0.9:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(series, errors="coerce")
    return parsed if parsed.notna().sum() >= 0.9 * series.notna().sum() else None


def normalise_types(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        s = df[col]
        if s.dtype == object:
            parsed = _try_datetime(s)
            if parsed is not None:
                df[col] = parsed
            else:  # mixed-type object columns break Parquet: store as text
                df[col] = s.map(lambda v: None if pd.isna(v) else str(v))
    return df


def _kind(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_numeric_dtype(s):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    n, uniq = s.notna().sum(), s.nunique(dropna=True)
    return "categorical" if uniq <= 50 or (n and uniq / n < 0.5) else "text"


def profile_dataframe(df: pd.DataFrame) -> dict:
    cols, metrics, dates, warns = [], [], [], []
    for name in df.columns:
        s = df[name]
        kind = _kind(s)
        info = {"name": name, "kind": kind, "dtype": str(s.dtype),
                "missing": int(s.isna().sum()),
                "missing_pct": round(float(s.isna().mean()) * 100, 2),
                "unique": int(s.nunique(dropna=True))}
        if kind == "numeric" and s.notna().any():
            info.update(min=float(s.min()), max=float(s.max()), mean=float(s.mean()))
            if not re.search(r"(^|[ _-])(id|code|zip|phone)$", name.lower()):
                metrics.append(name)
        if kind == "datetime" and s.notna().any():
            info.update(date_min=s.min().isoformat(), date_max=s.max().isoformat())
            dates.append(name)
        if info["missing_pct"] > 30:
            warns.append(f"Column '{name}' is {info['missing_pct']}% empty")
        cols.append(info)
    dup = int(df.duplicated().sum())
    if dup:
        warns.append(f"{dup} fully duplicated rows")
    return json_safe({"rows": int(len(df)), "column_count": int(df.shape[1]),
                      "duplicate_rows": dup, "columns": cols, "metric_candidates": metrics,
                      "date_candidates": dates, "warnings": warns})


def ingest(raw_path: Path, ext: str, parquet_path: Path) -> dict:
    df = normalise_types(read_table(raw_path, ext))
    df.to_parquet(parquet_path, index=False)
    return profile_dataframe(df)


def insights(parquet_path: str) -> dict:
    df = pd.read_parquet(parquet_path)
    out = {"row_count": int(len(df)), "top_categories": {}, "numeric": {}}
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_bool_dtype(s):
            continue
        if pd.api.types.is_numeric_dtype(s) and s.notna().any():
            out["numeric"][c] = {"sum": float(s.sum()), "mean": float(s.mean()),
                                 "max": float(s.max()), "min": float(s.min())}
        elif not pd.api.types.is_datetime64_any_dtype(s) and s.nunique() <= 30:
            out["top_categories"][c] = {str(k): int(v) for k, v in
                                        s.value_counts().head(5).items()}
    return json_safe(out)

