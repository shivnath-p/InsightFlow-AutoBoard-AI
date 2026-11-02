"""Bounded agent pipeline (a LangGraph-style graph written as plain, testable functions).

intake -> plan (LLM, else heuristic) -> validate plan -> analyze (DuckDB) -> verify (pandas
recompute) -> narrate (LLM draft, code-verified; else deterministic) -> finalize.

Hard limits: MAX_AGENT_STEPS steps, one re-plan, partial result instead of guessing.
Numbers always come from code. The LLM only chooses metrics and phrases verified facts.
"""
import logging
import re
import time

from pydantic import ValidationError

from . import config, llm
from .analytics import AnalyticsError, AnalyticsSession, recompute_fact, validate_request
from .schemas import MetricRequest, Plan
from .verify import fmt, verify_claims

log = logging.getLogger("insightflow.agent")

STOPWORDS = {"the", "a", "an", "of", "by", "per", "for", "in", "on", "is", "are", "what", "which",
             "show", "me", "total", "how", "much", "many", "has", "have", "with", "and", "to",
             "highest", "lowest", "top", "best", "worst", "most", "least", "average", "mean",
             "each", "all", "over", "time", "trend", "my", "our", "give", "get", "number"}
TREND_WORDS = ("trend", "over time", "monthly", "per month", "by month", "month by month",
               "daily", "per day", "by day", "yearly", "per year", "by year", "weekly")
LOW_WORDS = ("lowest", "least", "worst", "smallest", "bottom", "fewest", "minimum")
HIGH_WORDS = ("highest", "most", "top", "best", "largest", "biggest", "maximum")


class Run:
    """Mutable run state with step budget and a user-readable trace."""

    def __init__(self):
        self.steps, self.trace, self.warnings = 0, [], []

    def step(self, name: str, detail: str = ""):
        self.steps += 1
        if self.steps > config.MAX_AGENT_STEPS:
            raise RuntimeError("step budget exhausted")
        self.trace.append({"step": self.steps, "name": name, "detail": detail[:300],
                           "t": round(time.time(), 3)})


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") else tok


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower())]


def _match_columns(question: str, columns: list[dict]) -> list[dict]:
    q_tokens = _tokens(question)
    q_set = set(q_tokens)
    scored = []
    for col in columns:
        c_tokens = [t for t in _tokens(col["name"]) if t not in STOPWORDS] or _tokens(col["name"])
        hits = [t for t in c_tokens if t in q_set]
        if hits and len(hits) / len(c_tokens) >= 0.5:
            pos = min(q_tokens.index(h) for h in hits)
            scored.append((-len(hits) / len(c_tokens), pos, col))
    return [c for _, _, c in sorted(scored, key=lambda x: (x[0], x[1]))]


def heuristic_plan(question: str, profile: dict) -> Plan:
    """Deterministic planner used when no LLM key is set, or as the re-plan fallback."""
    q = question.lower()
    columns = profile["columns"]
    matched = _match_columns(q, columns)
    numeric = [c for c in matched if c["kind"] == "numeric"]
    cats = [c for c in matched if c["kind"] in ("categorical", "text", "boolean")]
    dates = [c for c in columns if c["kind"] == "datetime"]
    is_count = bool(re.search(r"\bhow many\b|\bcount\b|\bnumber of\b", q))
    agg = "avg" if re.search(r"\baverage\b|\bavg\b|\bmean\b", q) else "sum"
    order = "asc" if any(w in q for w in LOW_WORDS) else "desc"
    time_grain = None
    if any(w in q for w in TREND_WORDS) and dates:
        time_grain = "year" if "year" in q else "day" if re.search(r"daily|per day|by day", q) \
            else "month"
    # dimension: text after "by/per/each", otherwise a categorical column named in the question
    dims = []
    by = re.search(r"\b(?:by|per|each|across)\s+([a-z0-9_ ]+)", q)
    if by:
        dims = [c for c in _match_columns(by.group(1), columns)
                if c["kind"] in ("categorical", "text", "boolean")][:1]
    if not dims and cats and (any(w in q for w in HIGH_WORDS + LOW_WORDS) or "which" in q):
        dims = cats[:1]
    if is_count:
        metric, agg = "*", "count"
        if cats and not dims:
            dims = cats[:1]
    elif numeric:
        metric = numeric[0]["name"]
    else:
        candidates = profile.get("metric_candidates", [])
        if len(candidates) == 1:
            metric = candidates[0]
        else:
            return Plan(clarification="Which numeric column should I measure?",
                        options=candidates[:8])
    request = MetricRequest(
        metric=metric, agg=agg, dimensions=[d["name"] for d in dims][:2 - (1 if time_grain else 0)],
        time_column=dates[0]["name"] if time_grain else None, time_grain=time_grain, order=order,
        limit=50 if time_grain else (5 if dims else 20))
    return Plan(requests=[request])


def _schema_block(profile: dict) -> str:
    """Schema only: column names, kinds and date ranges. No cell values leave the server."""
    lines = [f"rows={profile['rows']}"]
    for c in profile["columns"]:
        extra = f" range={c['date_min'][:10]}..{c['date_max'][:10]}" if c.get("date_min") else ""
        lines.append(f"- {c['name']} [{c['kind']}]{extra}")
    return "\n".join(lines)


def make_plan(run: Run, question: str, profile: dict, egress: list) -> Plan:
    plan = None
    if llm.llm_enabled():
        run.step("plan", "LLM planner")
        obj, meta = llm.generate_json(
            llm.SYSTEM_PLANNER, {"schema": _schema_block(profile), "question": question},
            "plan")
        egress.append(meta)
        if obj is not None:
            try:
                plan = Plan(**obj)
            except ValidationError:
                run.warnings.append("LLM plan did not match the required schema; used fallback.")
    if plan is None:
        run.step("plan", "heuristic planner")
        plan = heuristic_plan(question, profile)
    return plan


def validated_requests(run: Run, plan: Plan, profile: dict) -> list[MetricRequest]:
    run.step("validate_plan", f"{len(plan.requests)} request(s)")
    good = []
    for req in plan.requests:
        try:
            validate_request(req, profile)
            good.append(req)
        except AnalyticsError as exc:
            run.warnings.append(f"Dropped an invalid metric request: {exc}")
    return good


def deterministic_claims(facts: list[dict]) -> list[dict]:
    claims = []
    for f in facts:
        p, rows, fid = f["params"], f["rows"], f["fact_id"]
        if not rows or rows[0]["value"] is None:
            claims.append({"text": f"{f['description']} has no matching data.", "fact_ids": [fid]})
            continue
        if not p["dimensions"] and not p["time_column"]:
            claims.append({"text": f"{f['description']} is {fmt(rows[0]['value'])} "
                                   f"across {fmt(f['source_rows'])} rows.", "fact_ids": [fid]})
        elif p["time_column"]:
            s = f.get("summary")
            first, last = rows[0], rows[-1]
            text = (f"{f['description']} was {fmt(first['value'])} in "
                    f"{list(first['labels'].values())[-1]} and {fmt(last['value'])} in "
                    f"{list(last['labels'].values())[-1]}")
            text += f" ({fmt(s['change_pct'])}% change)." if s else "."
            claims.append({"text": text, "fact_ids": [fid]})
        else:
            top = rows[0]
            word = "highest" if p["order"] == "desc" else "lowest"
            label = " / ".join(str(v) for v in top["labels"].values())
            text = f"For {f['description']}, the {word} group is {label} with {fmt(top['value'])}"
            if top.get("share_pct") is not None:
                text += f" ({fmt(top['share_pct'])}% of the total)"
            claims.append({"text": text + ".", "fact_ids": [fid]})
    return claims


def narrate(run: Run, question: str, facts: list[dict], egress: list) -> list[dict]:
    run.step("narrate")
    fallback = deterministic_claims(facts)
    if not llm.llm_enabled() or not facts or not config.LLM_SHARE_RESULTS:
        return fallback
    slim = [{k: f[k] for k in ("fact_id", "description", "rows", "groups", "source_rows",
                               "summary") if k in f} for f in facts]
    obj, meta = llm.generate_json(llm.SYSTEM_NARRATOR,
                                  {"facts": str(slim), "question": question}, "narrate")
    egress.append(meta)
    claims = (obj or {}).get("claims")
    if isinstance(claims, list) and claims:
        claims = [{"text": str(c.get("text", "")), "fact_ids": [str(x) for x in
                  c.get("fact_ids", [])]} for c in claims[:6] if isinstance(c, dict)]
        problems = verify_claims(claims, facts)
        if not problems:
            return claims
        run.warnings.append("LLM narrative rejected by the number verifier ("
                            + "; ".join(problems[:2]) + "); showing verified template text.")
    elif obj is not None:
        run.warnings.append("LLM narrative had an unexpected shape; showing template text.")
    return fallback


def run_agent(question: str, profile: dict, parquet_path: str) -> dict:
    """Pure function: returns everything the router needs to persist. No DB access here."""
    run, egress = Run(), []
    result = {"status": "failed", "plan": {}, "facts": [], "claims": [], "clarification": None,
              "options": [], "egress": egress}
    try:
        run.step("intake", "question accepted")
        plan = make_plan(run, question, profile, egress)
        requests = validated_requests(run, plan, profile)
        if not requests and plan.clarification is None:
            run.step("replan", "heuristic fallback")  # the single allowed re-plan
            plan = heuristic_plan(question, profile)
            requests = validated_requests(run, plan, profile)
        result["plan"] = plan.model_dump()
        if not requests:
            run.step("clarify")
            result.update(status="needs_input", options=plan.options,
                          clarification=plan.clarification or
                          "I could not map the question to your columns. Which column and "
                          "grouping should I use?")
            return result | {"warnings": run.warnings, "trace": run.trace}

        run.step("analyze", f"{len(requests)} query(ies)")
        facts = []
        with AnalyticsSession(parquet_path, profile) as session:
            for req in requests:
                try:
                    facts.append(session.run(req))
                except AnalyticsError as exc:
                    run.warnings.append(f"A query failed: {exc}")
        run.step("verify", "recomputing numbers with pandas")
        verified = []
        for fact in facts:
            try:
                ok, msg = recompute_fact(parquet_path, fact)
            except Exception as exc:
                log.warning("recompute failed for %s: %s", fact["fact_id"], type(exc).__name__)
                ok, msg = False, "recomputation error"
            if ok:
                fact["verified"] = True
                verified.append(fact)
            else:
                run.warnings.append(f"{fact['fact_id']} dropped, verification failed: {msg}")
        result["facts"] = verified
        if not verified:
            result["status"] = "failed"
            return result | {"warnings": run.warnings, "trace": run.trace}
        result["claims"] = narrate(run, question, verified, egress)
        run.step("finalize")
        result["status"] = "done" if len(verified) == len(requests) else "partial"
    except RuntimeError as exc:  # step budget -> partial result, never a guess
        run.warnings.append(str(exc))
        result["status"] = "partial" if result["facts"] else "failed"
    except Exception:
        log.exception("agent run crashed")
        run.warnings.append("Internal error while analysing this question.")
        result["status"] = "failed"
    return result | {"warnings": run.warnings, "trace": run.trace}
