"""Orchestrator for the weekly update and the one-time backfill audit.

  python pipeline/run.py --mode weekly             # research last 10 days, verify, draft (default)
  python pipeline/run.py --mode weekly --dry-run   # no database writes; print what would happen
  python pipeline/run.py --mode backfill --limit 10  # V&V audit of existing published records
"""
import argparse
import datetime as dt
import json
import os
import sys
import traceback

import requests

sys.path.insert(0, os.path.dirname(__file__))
from common import (CFG, rpc, fetch_codebook, fetch_all_records, normalize, schema_issues,  # noqa: E402
                    source_issues, possible_duplicates)
from research import research  # noqa: E402
from verify import verify  # noqa: E402

MAX_REVISIONS = 1


def record_verification(rid, v, dry):
    if dry:
        return
    rpc("pipeline_record_verification", p_id=rid, p_verdict=v["verdict"], p_reviewer=v["reviewer"],
        p_summary=v["summary"], p_checks=v["checks"] + [{"id": "S", "name": "sources_consulted",
                                                         "result": "info", "detail": ", ".join(v["sources_consulted"])}],
        p_changes={"fields": v["proposed_changes"], "sources": v["proposed_sources"]}
        if (v["proposed_changes"] or v["proposed_sources"]) else None)


def process_candidate(rec, codebook, existing, dry, log):
    sources = rec.pop("_sources", [])
    hard = schema_issues(rec, codebook) + source_issues(sources)
    title = rec.get("title") or "(untitled)"
    rid = None
    if hard:  # the database would reject it; the research agent must fix these, not the verifier
        log.append({"title": title, "outcome": "skipped", "why": "; ".join(hard)})
        return "skipped"
    strong = [d for d in possible_duplicates({**rec, "_sources": sources}, existing)
              if d["shared_urls"] or d["title_similarity"] >= 0.9]
    if strong:
        log.append({"title": title, "outcome": "skipped",
                    "why": "likely duplicate of " + ", ".join(d["incident_id"] for d in strong)})
        return "skipped"
    if not dry:
        try:
            rid = rpc("pipeline_insert_draft", rec={k: v for k, v in rec.items() if v is not None}, srcs=sources)
        except RuntimeError as e:
            log.append({"title": title, "outcome": "skipped", "why": f"database rejected draft: {e}"})
            return "skipped"
    rid = rid or "DRY-RUN"

    v = verify(rec, sources, codebook, existing)
    record_verification(rid, v, dry)
    history = [v["verdict"]]
    rounds = 0
    while v["verdict"] == "REVISE" and (v["proposed_changes"] or v["proposed_sources"]) and rounds < MAX_REVISIONS:
        rounds += 1
        rec = normalize({**rec, **(v["proposed_changes"] or {})})
        sources = v["proposed_sources"] or sources
        if not dry:
            rpc("pipeline_apply_changes", p_id=rid, p_changes={k: val for k, val in rec.items() if val is not None},
                p_srcs=v["proposed_sources"])
        v = verify(rec, sources, codebook, existing)
        record_verification(rid, v, dry)
        history.append(v["verdict"])

    published = False
    if (v["verdict"] == "PASS" and CFG["AUTO_PUBLISH"] and rec.get("confidence_tier") in CFG["AUTO_PUBLISH_TIERS"]
            and not dry):
        rpc("pipeline_publish", p_id=rid)
        published = True
    log.append({"incident_id": rid, "title": title, "tier": rec.get("confidence_tier"),
                "event_class": rec.get("event_class"), "verdicts": " → ".join(history),
                "outcome": "published" if published else v["verdict"], "summary": v["summary"],
                "failed_checks": [f"{c.get('id')} {c.get('name')}: {c.get('detail','')[:160]}"
                                  for c in v["checks"] if c.get("result") == "fail"]})
    return "published" if published else v["verdict"]


def report_md(mode, window, stats, log, research_urls):
    lines = [f"# Agent incident tracker: {mode} run {dt.date.today().isoformat()}", ""]
    if window:
        lines.append(f"Disclosure window: {window[0]} to {window[1]}")
    lines.append("Results: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    lines.append("")
    groups = [("published", "Published automatically"), ("PASS", "Passed V&V, awaiting your approval"),
              ("REVISE", "Needs human review"), ("REJECT", "Rejected by V&V"), ("skipped", "Skipped before V&V")]
    for key, head in groups:
        items = [x for x in log if x["outcome"] == key]
        if not items:
            continue
        lines += [f"## {head} ({len(items)})", ""]
        for x in items:
            head_id = f"{x['incident_id']} " if x.get("incident_id") else ""
            meta = ", ".join(v for v in (x.get("tier"), x.get("event_class")) if v)
            lines.append(f"- **{head_id}{x['title']}**" + (f" ({meta})" if meta else "")
                         + (f", verdicts: {x['verdicts']}" if x.get("verdicts") else ""))
            if x.get("summary"):
                lines.append(f"  - {x['summary']}")
            if x.get("why"):
                lines.append(f"  - {x['why']}")
            for f in x.get("failed_checks", []):
                lines.append(f"  - ✗ {f}")
        lines.append("")
    if stats.get("PASS"):
        lines += ["To publish a passed record, run in the Supabase SQL editor:",
                  "`select public.pipeline_publish('AAB-YYYY-NNN');`", ""]
    return "\n".join(lines)


def open_issue(title, body):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return
    requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                  json={"title": title, "body": body[:60000], "labels": ["tracker-review"]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["weekly", "backfill", "manual"], default="weekly")
    ap.add_argument("--since", help="window start YYYY-MM-DD (default: 10 days ago)")
    ap.add_argument("--until", help="window end YYYY-MM-DD (default: today)")
    ap.add_argument("--limit", type=int, default=0, help="backfill: max records to audit this run")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    codebook, existing = fetch_codebook(), fetch_all_records()
    log, research_urls, window = [], [], None
    stats = {"candidates": 0, "published": 0, "PASS": 0, "REVISE": 0, "REJECT": 0, "skipped": 0}

    if a.mode in ("weekly", "manual"):
        end = dt.date.fromisoformat(a.until) if a.until else dt.date.today()
        start = dt.date.fromisoformat(a.since) if a.since else end - dt.timedelta(days=10)  # 3-day overlap
        window = (start.isoformat(), end.isoformat())
        cands, research_urls = research(window[0], window[1], codebook, existing)
        stats["candidates"] = len(cands)
        for rec in cands:
            try:
                outcome = process_candidate(rec, codebook, existing, a.dry_run, log)
            except Exception as e:  # keep going; one bad record must not sink the run
                outcome = "skipped"
                log.append({"title": rec.get("title", "?"), "outcome": "skipped", "why": f"error: {e}"})
                traceback.print_exc()
            stats[outcome] = stats.get(outcome, 0) + 1
            existing.append({"incident_id": "this-run", "title": rec.get("title"),
                             "disclosure_date": rec.get("disclosure_date"), "sources": []})
    else:  # backfill audit: verify published legacy records without changing them
        targets = [e for e in existing if e.get("review_status") == "legacy"]
        targets = [t for t in targets if not t.get("n_verifications")]  # resume where the last run stopped
        if a.limit:
            targets = targets[:a.limit]
        for e in targets:
            rec = normalize(e)
            srcs = [{"citation": s["citation"], "url": s.get("url"), "is_primary": s.get("is_primary", False)}
                    for s in e.get("sources") or []]
            others = [x for x in existing if x["incident_id"] != e["incident_id"]]
            v = verify(rec, srcs, codebook, others)
            record_verification(e["incident_id"], v, a.dry_run)
            stats[v["verdict"]] += 1
            log.append({"incident_id": e["incident_id"], "title": e["title"], "tier": e.get("confidence_tier"),
                        "event_class": e.get("event_class"), "verdicts": v["verdict"], "outcome": v["verdict"],
                        "summary": v["summary"],
                        "failed_checks": [f"{c.get('id')} {c.get('name')}: {c.get('detail','')[:160]}"
                                          for c in v["checks"] if c.get("result") == "fail"]})
        stats["candidates"] = len(targets)

    md = report_md(a.mode, window, stats, log, research_urls)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    if not a.dry_run:
        rpc("pipeline_log_run", p={"started_at": started, "mode": a.mode,
                                   "window_start": window[0] if window else None, "window_end": window[1] if window else None,
                                   "candidates": stats["candidates"], "drafted": stats["candidates"] - stats["skipped"],
                                   "passed": stats["PASS"], "needs_review": stats["REVISE"], "rejected": stats["REJECT"],
                                   "published": stats["published"], "report": md})
        if stats["candidates"]:
            open_issue(f"Tracker {a.mode} review: {dt.date.today().isoformat()} "
                       f"({stats['PASS']} passed, {stats['REVISE']} need review)", md)


if __name__ == "__main__":
    main()
