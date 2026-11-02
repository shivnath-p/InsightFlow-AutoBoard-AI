"""Claim verification: every number in a narrative must match a referenced fact."""
import re

NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?%?")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
# links, markdown links and HTML-ish markup have no place in a number-backed finding; a model
# steered by hostile cell text could otherwise smuggle a phishing link into a report
LINKISH = re.compile(r"https?://|www\.|\]\(|<\s*[a-z/]|javascript:|data:text", re.I)
MAX_CLAIM_CHARS = 400
CAUSAL = re.compile(r"\b(because|due to|caused?|causing|reason|resulted? in|driven by|"
                    r"thanks to|leads? to|led to)\b", re.I)


def fmt(value) -> str:
    """Canonical number format used by the deterministic narrator."""
    if value is None:
        return "n/a"
    value = float(value)
    return f"{int(value):,}" if value == int(value) else f"{value:,.2f}"


def allowed_numbers(fact: dict) -> set[float]:
    nums = {float(fact["groups"]), float(fact["source_rows"])}
    for row in fact["rows"]:
        if row.get("value") is not None:
            nums.add(float(row["value"]))
        if row.get("share_pct") is not None:
            nums.add(float(row["share_pct"]))
    for v in (fact.get("summary") or {}).values():
        nums.add(float(v))
    return nums


def _labels(fact: dict) -> list[str]:
    return [str(v) for row in fact["rows"] for v in row["labels"].values()]


def check_text(text: str, facts: list[dict]) -> list[str]:
    """Return a list of problems (empty list = text is supported by the facts)."""
    problems = []
    if len(text) > MAX_CLAIM_CHARS:
        problems.append("claim is too long")
    if LINKISH.search(text):
        problems.append("links or markup are not allowed in findings")
    if CAUSAL.search(text):
        problems.append("causal wording is not supported by the data")
    cleaned = text
    for label in sorted({lab for f in facts for lab in _labels(f)}, key=len, reverse=True):
        if label:
            cleaned = cleaned.replace(label, " ")
    cleaned = ISO_DATE.sub(" ", cleaned)
    allowed = set().union(*(allowed_numbers(f) for f in facts)) if facts else set()
    for token in NUMBER.findall(cleaned):
        raw = token.rstrip("%").replace(",", "")
        try:
            num = float(raw)
        except ValueError:
            continue
        if num.is_integer() and 0 <= num <= 10 and "." not in raw and not token.endswith("%"):
            continue  # ordinals such as "top 3"
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        if not any(abs(num - a) <= tol for a in allowed):
            problems.append(f"number {token} is not backed by the referenced facts")
    return problems


def verify_claims(claims: list[dict], facts: list[dict]) -> list[str]:
    by_id = {f["fact_id"]: f for f in facts}
    problems = []
    for i, claim in enumerate(claims):
        ids = claim.get("fact_ids") or []
        text = claim.get("text", "")
        if not text or not ids:
            problems.append(f"claim {i + 1} has no text or no fact_ids")
            continue
        if any(fid not in by_id for fid in ids):
            problems.append(f"claim {i + 1} references an unknown fact")
            continue
        problems += [f"claim {i + 1}: {p}" for p in check_text(text, [by_id[x] for x in ids])]
    return problems
