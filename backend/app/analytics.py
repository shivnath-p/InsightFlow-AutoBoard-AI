"""Semantic layer + hardened DuckDB executor.

The LLM only ever produces a MetricRequest (validated JSON). This module validates it against
the dataset profile (column allow-list), compiles SQL with quoted identifiers and bound
parameters, and runs it on a locked-down in-process DuckDB connection.
"""
import hashlib
import json
import math
import threading
from datetime import datetime, timezone

import duckdb
import pandas as pd

from . import config
from .schemas import MetricRequest


class AnalyticsError(ValueError):
    pass


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cols(profile: dict) -> dict[str, dict]:
    return {c["name"]: c for c in profile["columns"]}


def validate_request(req: MetricRequest, profile: dict) -> None:
    cols = _cols(profile)
    if req.metric == "*":
        if req.agg != "count":
            raise AnalyticsError("metric '*' is only valid with agg=count")
    else:
        if req.metric not in cols:
            raise AnalyticsError(f"unknown column: {req.metric}")
        kind = cols[req.metric]["kind"]
        if req.agg in ("sum", "avg", "min", "max") and kind != "numeric":
            raise AnalyticsError(f"{req.agg} needs a numeric column, '{req.metric}' is {kind}")
    for d in req.dimensions:
        if d not in cols:
            raise AnalyticsError(f"unknown dimension: {d}")
        if cols[d]["kind"] not in ("categorical", "text", "boolean"):
            raise AnalyticsError(f"'{d}' cannot be used as a dimension")
    if bool(req.time_column) != bool(req.time_grain):
        raise AnalyticsError("time_column and time_grain must be given together")
    if req.time_column:
        if req.time_column not in cols or cols[req.time_column]["kind"] != "datetime":
            raise AnalyticsError(f"'{req.time_column}' is not a date column")
    if len(req.dimensions) + (1 if req.time_column else 0) > 2:
        raise AnalyticsError("at most two grouping fields are allowed")
    for f in req.filters:
        if f.column not in cols:
            raise AnalyticsError(f"unknown filter column: {f.column}")
        kind = cols[f.column]["kind"]
        values = f.value if isinstance(f.value, list) else [f.value]
        if (f.op == "in") != isinstance(f.value, list):
            raise AnalyticsError("'in' needs a list, other operators need a single value")
        if len(values) > 50:
            raise AnalyticsError("too many values in filter")
        if kind == "numeric" and not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                         for v in values):
            raise AnalyticsError(f"filter on numeric '{f.column}' needs numbers")
        if kind in ("categorical", "text") and f.op not in ("=", "!=", "in"):
            raise AnalyticsError("text columns only support =, != and in")


def compile_sql(req: MetricRequest, profile: dict) -> tuple[str, list]:
    cols = _cols(profile)
    params: list = []
    where = []
    for f in req.filters:
        col = _q(f.column)
        is_dt = cols[f.column]["kind"] == "datetime"
        ph = "CAST(? AS TIMESTAMP)" if is_dt else "?"
        if f.op == "in":
            where.append(f"{col} IN ({', '.join([ph] * len(f.value))})")
            params.extend(f.value)
        else:
            where.append(f"{col} {f.op} {ph}")
            params.append(f.value)

    group, labels = [], []
    for d in req.dimensions:
        group.append(f"COALESCE(CAST({_q(d)} AS VARCHAR), '(missing)')")
        labels.append(d)
    if req.time_column:  # grain is a Literal, safe to inline
        group.append(f"strftime(date_trunc('{req.time_grain}', {_q(req.time_column)}), '%Y-%m-%d')")
        labels.append(f"{req.time_column}_{req.time_grain}")

    col = "*" if req.metric == "*" else _q(req.metric)
    agg = {"sum": f"SUM({col})", "avg": f"AVG({col})", "min": f"MIN({col})",
           "max": f"MAX({col})", "count": f"COUNT({col})",
           "count_distinct": f"COUNT(DISTINCT {col})"}[req.agg]
    if group and req.agg in ("sum", "count"):
        total = f"SUM({agg}) OVER ()"
    else:
        total = "NULL"
    select = [f"{g} AS g{i}" for i, g in enumerate(group)]
    select += [f"{agg} AS value", "COUNT(*) AS n", f"{total} AS total",
               "SUM(COUNT(*)) OVER () AS total_n" if group else "COUNT(*) AS total_n"]
    # nosec B608: identifiers go through _q(), values are bound parameters and the time grain is
    # a Literal, so nothing user-controlled is concatenated (see test_sql_injection_is_impossible)
    sql = f"SELECT {', '.join(select)} FROM data"  # nosec B608
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(group)))
        if req.time_column:  # newest periods first so LIMIT keeps the latest; reversed later
            sql += f" ORDER BY g{len(group) - 1} DESC"
        else:
            sql += f" ORDER BY value {req.order.upper()} NULLS LAST, g0 ASC"
    sql += f" LIMIT {int(req.limit)}"
    return sql, params


def describe(req: MetricRequest) -> str:
    what = "rows" if req.metric == "*" else req.metric
    text = f"{req.agg.upper().replace('_', ' ')} of {what}"
    by = list(req.dimensions) + ([f"{req.time_column} ({req.time_grain})"] if req.time_column
                                 else [])
    if by:
        text += " by " + " and ".join(by)
    if req.filters:
        text += " (filtered)"
    return text


class AnalyticsSession:
    """One locked-down in-process DuckDB connection per agent run."""

    def __init__(self, parquet_path: str, profile: dict):
        self.parquet_path, self.profile = parquet_path, profile
        self.con = None
        self.counter = 0

    def __enter__(self):
        con = duckdb.connect(":memory:")
        con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
        con.execute(f"SET threads={int(config.DUCKDB_THREADS)}")
        con.execute("CREATE TABLE data AS SELECT * FROM read_parquet(?)", [self.parquet_path])
        con.execute("SET enable_external_access=false")  # no files / network after loading
        con.execute("SET lock_configuration=true")       # settings cannot be changed again
        self.con = con
        return self

    def __exit__(self, *exc):
        if self.con:
            self.con.close()

    def run(self, req: MetricRequest) -> dict:
        validate_request(req, self.profile)
        sql, params = compile_sql(req, self.profile)
        timer = threading.Timer(config.QUERY_TIMEOUT_SECONDS, self.con.interrupt)
        timer.start()
        try:
            rows = self.con.execute(sql, params).fetchall()
        except duckdb.InterruptException:
            raise AnalyticsError("query timed out")
        except duckdb.Error as exc:
            raise AnalyticsError(f"query failed: {type(exc).__name__}")
        finally:
            timer.cancel()
        self.counter += 1
        return self._to_fact(f"F{self.counter}", req, sql, params, rows)

    def _to_fact(self, fact_id: str, req: MetricRequest, sql: str, params: list,
                 rows: list) -> dict:
        labels = list(req.dimensions) + ([f"{req.time_column}_{req.time_grain}"]
                                         if req.time_column else [])
        out_rows = []
        ngroups = len(labels)
        if req.time_column:
            rows = list(reversed(rows))  # chronological order for charts
        source_rows = int(rows[0][ngroups + 3]) if rows else 0
        values_ok = all(r[ngroups] is not None and r[ngroups] >= 0 for r in rows)
        for r in rows:
            value = r[ngroups]
            value = None if value is None else (float(value) if not isinstance(value, int)
                                                else value)
            row = {"labels": {labels[i]: r[i] for i in range(ngroups)}, "value": value}
            total = r[ngroups + 2]
            if total and total > 0 and values_ok and value is not None:
                row["share_pct"] = round(100 * float(value) / float(total), 2)
            out_rows.append(row)
        fact = {"fact_id": fact_id, "tool": "calculate_metric", "description": describe(req),
                "params": req.model_dump(), "rows": out_rows, "groups": len(out_rows),
                "source_rows": source_rows, "verified": False,
                "query_hash": hashlib.sha256((sql + json.dumps(params, default=str)).encode())
                .hexdigest()[:16],
                "ts": datetime.now(timezone.utc).isoformat(),
                "chart": _chart_for(req, len(out_rows))}
        if req.time_column and len(out_rows) >= 2:
            first, last = out_rows[0]["value"], out_rows[-1]["value"]
            if first not in (None, 0) and last is not None:
                fact["summary"] = {"first": first, "last": last,
                                   "change_pct": round(100 * (last - first) / abs(first), 2)}
        return fact


def _chart_for(req: MetricRequest, n: int) -> str:
    if not req.dimensions and not req.time_column:
        return "kpi"
    if req.time_column:
        return "line"
    return "bar" if n > 1 else "kpi"


def recompute_fact(parquet_path: str, fact: dict) -> tuple[bool, str]:
    """Independent pandas re-computation of a fact (the 'numeric verification' step).
    Uses a different code path than DuckDB, so a bug in either shows up as a mismatch."""
    req = MetricRequest(**fact["params"])
    df = pd.read_parquet(parquet_path)
    for f in req.filters:
        s = df[f.column]
        val = f.value
        if pd.api.types.is_datetime64_any_dtype(s):
            val = [pd.Timestamp(v) for v in val] if isinstance(val, list) else pd.Timestamp(val)
        ops = {"=": lambda: s == val, "!=": lambda: s != val, ">": lambda: s > val,
               ">=": lambda: s >= val, "<": lambda: s < val, "<=": lambda: s <= val,
               "in": lambda: s.isin(val)}
        df = df[ops[f.op]()]
    keys = []
    for d in req.dimensions:
        keys.append(df[d].astype("string").fillna("(missing)").str.lower())
    if req.time_column:
        freq = {"day": "D", "month": "M", "year": "Y"}[req.time_grain]
        per = df[req.time_column].dt.to_period(freq).dt.start_time.dt.strftime("%Y-%m-%d")
        keys.append(per)
    metric = df[req.metric] if req.metric != "*" else None

    def agg(frame_idx):
        if req.metric == "*":
            return float(len(frame_idx))
        s = metric.loc[frame_idx]
        return float({"sum": s.sum, "avg": s.mean, "min": s.min, "max": s.max, "count": s.count,
                      "count_distinct": s.nunique}[req.agg]())

    if not keys:
        expected = {(): agg(df.index)}
    else:
        grouped = df.groupby(keys, dropna=False).groups
        expected = {(k if isinstance(k, tuple) else (k,)): agg(idx) for k, idx in grouped.items()}
    labels = list(req.dimensions) + ([f"{req.time_column}_{req.time_grain}"]
                                     if req.time_column else [])
    for row in fact["rows"]:
        key = tuple(str(row["labels"][name]).lower() for name in labels)
        if key not in expected:
            return False, f"group {key} missing in recomputation"
        want, got = expected[key], row["value"]
        if got is None or not math.isclose(want, float(got), rel_tol=1e-6, abs_tol=1e-6):
            return False, f"value mismatch for {key}: {got} vs {want}"
    return True, "ok"
