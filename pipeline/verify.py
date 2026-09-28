"""Verification & validation agent: independent, skeptical review of one drafted record.

Layer 1 (code, deterministic): schema/codebook validation, source hygiene, live HTTP check of every URL,
duplicate screen against the full database.
Layer 2 (model, independent): a different model re-derives every claim from the sources with its own web
search and fetch, and returns PASS / REVISE / REJECT with per-check findings.
Code-level hard failures override the model: a record that fails schema checks can never PASS.
"""
import json

from common import (CFG, claude, extract_json, prompt, codebook_text, schema_issues, source_issues,
                    check_urls, possible_duplicates)

SYSTEM = ("You are an independent fact-checker and data-quality auditor. You distrust the draft you are given, "
          "verify every claim against sources you fetch yourself, and return strictly valid JSON.")


def precheck(rec, sources, codebook, existing):
    return {
        "schema_issues": schema_issues(rec, codebook),
        "source_issues": source_issues(sources),
        "url_checks": check_urls(sources),
        "possible_duplicates": possible_duplicates({**rec, "_sources": sources}, existing),
    }


def verify(rec, sources, codebook, existing):
    pre = precheck(rec, sources, codebook, existing)
    public = {k: v for k, v in rec.items() if not k.startswith("_")}
    user = prompt("verify", codebook=codebook_text(codebook),
                  precheck=json.dumps({k: v for k, v in pre.items() if k != "possible_duplicates"}, indent=1),
                  possible_duplicates=json.dumps(pre["possible_duplicates"], indent=1) or "[]",
                  record=json.dumps({"record": public, "sources": sources}, indent=1, default=str))
    text, urls = claude(SYSTEM, user, CFG["VERIFY_MODEL"], max_searches=8, max_fetches=12)
    try:
        v = extract_json(text)
        if not isinstance(v, dict):
            raise ValueError("verifier returned a non-object")
    except ValueError:
        v = {"verdict": "REVISE", "summary": "Verifier output could not be parsed; needs human review.", "checks": []}
    verdict = v.get("verdict") if v.get("verdict") in ("PASS", "REVISE", "REJECT") else "REVISE"

    # Deterministic overrides
    code_checks = []
    if pre["schema_issues"] or pre["source_issues"]:
        code_checks.append({"id": "C1", "name": "schema_and_sources", "result": "fail",
                            "detail": "; ".join(pre["schema_issues"] + pre["source_issues"])})
        if verdict == "PASS":
            verdict = "REVISE"
    else:
        code_checks.append({"id": "C1", "name": "schema_and_sources", "result": "pass", "detail": "all constraints met"})
    broken = [u for u in pre["url_checks"] if u["status"] not in ("ok", "blocked_for_bots")]
    ok_urls = [u for u in pre["url_checks"] if u["status"] == "ok"]
    code_checks.append({"id": "C2", "name": "urls_live", "result": "fail" if broken else ("pass" if ok_urls else "warn"),
                        "detail": json.dumps(pre["url_checks"])})
    if broken and verdict == "PASS":
        verdict = "REVISE"
    if not ok_urls and verdict == "PASS" and rec.get("confidence_tier") in ("T1", "T2"):
        # No source could be independently loaded by code; the model's fetch is the only evidence.
        code_checks[-1]["detail"] += " | no URL loaded outside the model; accepted on verifier fetch only"
    dupes = [d for d in pre["possible_duplicates"] if d["title_similarity"] >= 0.85 or d["shared_urls"]]
    code_checks.append({"id": "C3", "name": "duplicate_screen", "result": "warn" if dupes else "pass",
                        "detail": json.dumps(pre["possible_duplicates"])})

    return {
        "verdict": verdict,
        "summary": v.get("summary", ""),
        "checks": code_checks + (v.get("checks") or []),
        "proposed_changes": v.get("proposed_changes") or None,
        "proposed_sources": v.get("proposed_sources") or None,
        "sources_consulted": sorted(set((v.get("sources_consulted") or []) + urls)),
        "reviewer": CFG["VERIFY_MODEL"],
    }
