"""Research agent: finds and drafts new incident records for a date window."""
from common import CFG, claude, extract_json, prompt, codebook_text, normalize

SYSTEM = ("You are a meticulous security incident researcher. You cite primary sources, never invent facts, "
          "and return strictly valid JSON when asked.")


def research(window_start, window_end, codebook, existing):
    listing = "\n".join(f"- {e['incident_id']} | {e.get('disclosure_date')} | {e.get('title')}" for e in existing)
    user = prompt("research", window_start=window_start, window_end=window_end,
                  codebook=codebook_text(codebook), existing=listing or "(none)")
    text, urls = claude(SYSTEM, user, CFG["RESEARCH_MODEL"], max_searches=20, max_fetches=25)
    items = extract_json(text)
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items or []:
        rec = normalize(it.get("record", {}))
        rec["_sources"] = it.get("sources", [])
        out.append(rec)
    return out, urls
