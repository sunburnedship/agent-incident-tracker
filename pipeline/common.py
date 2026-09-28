"""Shared helpers: configuration, Supabase RPC, Claude API calls, deterministic checks."""
import datetime as dt
import difflib
import json
import os
import re
import time
from pathlib import Path

import requests

PROMPTS = Path(__file__).parent / "prompts"

# ---------------- configuration ----------------
CFG = {
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "SUPABASE_URL": os.environ.get("SUPABASE_URL", "").rstrip("/"),
    "SUPABASE_SERVICE_KEY": os.environ.get("SUPABASE_SERVICE_KEY", ""),
    "RESEARCH_MODEL": os.environ.get("RESEARCH_MODEL", "claude-sonnet-5"),
    "VERIFY_MODEL": os.environ.get("VERIFY_MODEL", "claude-opus-5-5"),
    "ANTHROPIC_BETA": os.environ.get("ANTHROPIC_BETA", "web-fetch-2025-09-10"),
    "AUTO_PUBLISH": os.environ.get("AUTO_PUBLISH", "false").lower() == "true",
    "AUTO_PUBLISH_TIERS": [t.strip() for t in os.environ.get("AUTO_PUBLISH_TIERS", "T1,T2").split(",") if t.strip()],
}

ENUMS = {
    "event_class": {"Realized incident", "Controlled-eval incident", "Demonstrated vulnerability"},
    "scope_fit": {"Core", "Adjacent"},
    "confidence_tier": {"T1", "T2", "T3", "T4"},
    "adversary_involved": {"Yes", "No", "Unknown"},
    "agent_config": {"Single agent", "Multi-agent", "Multi-agent/swarm", "N/A"},
    "autonomy_level": {"L1 Assistive", "L2 Tool-using w/ approval", "L3 Autonomous tool use",
                       "L4 Long-horizon / orchestration", "N/A", None},
    "human_in_loop": {"Yes", "No", "Partial", "Unknown", "N/A", None},
    "incident_date_precision": {"day", "month", "year", None},
    "disclosure_date_precision": {"day", "month", "year"},
}
REQUIRED = ["title", "summary", "event_class", "scope_fit", "confidence_tier", "disclosure_date",
            "disclosure_date_precision", "adversary_involved", "agent_config", "severity_realized"]
ARRAY_FIELDS = ["entry_vector", "failure_mechanism", "impact", "owasp_llm", "owasp_agentic", "mitre_atlas"]
NUMERIC_FIELDS = ["records_affected", "users_affected", "orgs_affected", "financial_loss_usd", "downtime_hours"]
RECORD_FIELDS = REQUIRED + ["incident_date", "incident_date_precision", "vendor_model", "product_framework", "sector",
                            "autonomy_level", "human_in_loop", "severity_potential", "remediation_status", "notes"] \
    + ARRAY_FIELDS + NUMERIC_FIELDS


def prompt(name, **kw):
    return (PROMPTS / f"{name}.md").read_text().format(**kw)


# ---------------- Supabase ----------------
def _sb_headers():
    key = CFG["SUPABASE_SERVICE_KEY"]
    h = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):
        h["Authorization"] = f"Bearer {key}"
    return h


def rpc(name, **params):
    r = requests.post(f"{CFG['SUPABASE_URL']}/rest/v1/rpc/{name}", headers=_sb_headers(),
                      data=json.dumps(params), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase RPC {name} failed ({r.status_code}): {r.text[:500]}")
    return r.json() if r.text else None


def fetch_codebook():
    r = requests.get(f"{CFG['SUPABASE_URL']}/rest/v1/agent_codebook?select=*", headers=_sb_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_all_records():
    rows = rpc("pipeline_all_records") or []
    return [json.loads(x) if isinstance(x, str) else x for x in rows]


# ---------------- Claude API ----------------
def claude(system, user, model, max_searches=10, max_fetches=15, max_tokens=16000):
    """Run one agent turn with server-side web search and fetch. Returns (text, urls_seen)."""
    headers = {"x-api-key": CFG["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    if CFG["ANTHROPIC_BETA"]:
        headers["anthropic-beta"] = CFG["ANTHROPIC_BETA"]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches},
             {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": max_fetches}]
    messages = [{"role": "user", "content": user}]
    text_parts, urls = [], []
    for _ in range(8):  # server tools may pause long turns; resume up to 8 times
        body = {"model": model, "max_tokens": max_tokens, "system": system, "tools": tools, "messages": messages}
        for attempt in range(4):
            r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, data=json.dumps(body), timeout=600)
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(20 * (attempt + 1)); continue
            break
        if r.status_code >= 300:
            raise RuntimeError(f"Claude API error {r.status_code}: {r.text[:800]}")
        resp = r.json()
        for block in resp.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
            if block.get("type") in ("web_fetch_tool_result", "web_search_tool_result"):
                urls.extend(re.findall(r'"url":\s*"([^"]+)"', json.dumps(block)))
        if resp.get("stop_reason") == "pause_turn":
            messages = messages + [{"role": "assistant", "content": resp["content"]}]
            continue
        break
    return "\n".join(text_parts), sorted(set(urls))


def _balanced(c, start):
    """Parse the JSON value that begins at c[start] ('{' or '['), or return None."""
    opener = c[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(c)):
        ch = c[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                if ch != closer:
                    return None
                try:
                    return json.loads(c[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def extract_json(text):
    """Return the first complete top-level JSON value, preferring fenced blocks (last one first)."""
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for c in fenced[::-1] + [text]:
        for i, ch in enumerate(c):
            if ch in "{[":
                v = _balanced(c, i)
                if v is not None:
                    return v
    raise ValueError("No JSON found in model output")


# ---------------- deterministic checks ----------------
def normalize(rec):
    """Coerce a drafted record into the database shape."""
    out = {k: rec.get(k) for k in RECORD_FIELDS}
    for k in ARRAY_FIELDS:
        v = out.get(k) or []
        out[k] = [x.strip() for x in (v.split(";") if isinstance(v, str) else v) if str(x).strip()]
    for k in ("incident_date", "disclosure_date"):
        if out.get(k) in ("", "null"):
            out[k] = None
    return out


def schema_issues(rec, codebook):
    """Hard failures: anything the database would reject, plus basic quality floors."""
    issues = []
    for k in REQUIRED:
        if rec.get(k) in (None, "", []):
            issues.append(f"missing required field {k}")
    for k, allowed in ENUMS.items():
        if rec.get(k) not in allowed:
            issues.append(f"{k}={rec.get(k)!r} not an allowed value")
    labels = {}
    for row in codebook:
        labels.setdefault(row["stage"], set()).add(row["label"])
    for stage in ("entry_vector", "failure_mechanism", "impact"):
        if not rec.get(stage):
            issues.append(f"{stage} is empty")
        for lab in rec.get(stage) or []:
            base = re.sub(r" \(attempted\)$", "", lab)
            if base not in labels.get(stage, set()):
                issues.append(f"{stage} label {lab!r} not in codebook")
    for k in ("severity_realized", "severity_potential"):
        v = rec.get(k)
        if v is not None and (not isinstance(v, int) or not 1 <= v <= 5):
            issues.append(f"{k}={v!r} must be an integer 1-5")
    try:
        d = dt.date.fromisoformat(rec["disclosure_date"])
        if d > dt.date.today():
            issues.append("disclosure_date is in the future")
        if rec.get("incident_date") and dt.date.fromisoformat(rec["incident_date"]) > d:
            issues.append("incident_date is after disclosure_date")
    except Exception:
        issues.append("dates must be ISO YYYY-MM-DD")
    for k in NUMERIC_FIELDS:
        v = rec.get(k)
        if v is not None and (not isinstance(v, (int, float)) or v < 0):
            issues.append(f"{k}={v!r} must be a non-negative number or null")
    if rec.get("title") and len(rec["title"]) > 120:
        issues.append("title longer than 120 characters")
    return issues


def source_issues(sources):
    issues = []
    if not sources:
        issues.append("no sources")
    for s in sources or []:
        if not s.get("citation"):
            issues.append("source without citation")
        if s.get("url") and not re.match(r"^https?://", s["url"]):
            issues.append(f"bad URL {s['url']!r}")
    if sources and not any(s.get("url") for s in sources):
        issues.append("no source has a URL")
    return issues


def check_urls(sources):
    """Independent HTTP check of every source URL (not via the model)."""
    results = []
    ua = {"User-Agent": "Mozilla/5.0 (compatible; AgentIncidentTracker/1.0; verification bot)"}
    for s in sources or []:
        url = s.get("url")
        if not url:
            continue
        try:
            r = requests.get(url, headers=ua, timeout=25, allow_redirects=True)
            code = r.status_code
            status = "ok" if code < 400 else ("blocked_for_bots" if code in (401, 403, 429) else "broken")
        except requests.RequestException as e:
            code, status = None, f"error: {type(e).__name__}"
        results.append({"url": url, "http": code, "status": status})
    return results


def possible_duplicates(rec, existing, threshold=0.72):
    """Flag existing records with similar titles or shared source URLs."""
    hits = []
    t = (rec.get("title") or "").lower()
    urls = {s.get("url") for s in rec.get("_sources", []) if s.get("url")}
    for e in existing:
        ratio = difflib.SequenceMatcher(None, t, (e.get("title") or "").lower()).ratio()
        shared = urls & {s.get("url") for s in (e.get("sources") or []) if s.get("url")}
        if ratio >= threshold or shared:
            hits.append({"incident_id": e["incident_id"], "title": e.get("title"),
                         "disclosure_date": e.get("disclosure_date"), "title_similarity": round(ratio, 2),
                         "shared_urls": sorted(shared)})
    return hits


def codebook_text(codebook):
    lines = []
    for stage in ("entry_vector", "failure_mechanism", "impact"):
        lines.append(f"{stage}:")
        for row in codebook:
            if row["stage"] == stage:
                xw = ", ".join(x for x in (row.get("owasp_llm"), row.get("owasp_agentic"), row.get("mitre_atlas")) if x)
                lines.append(f"  - {row['label']}: {row['definition']}" + (f" [{xw}]" if xw else ""))
    return "\n".join(lines)
