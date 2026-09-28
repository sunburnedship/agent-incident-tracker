# AI Agent Incident Tracker

Public dashboard (`index.html`, served by GitHub Pages), a Supabase database (`sql/`), and a weekly
research + verification pipeline (`pipeline/`, run by GitHub Actions).

## How a record gets published

```
Monday 6:00 a.m. Arizona ─► Research agent searches the last 10 days of disclosures
                            │  (3-day overlap with the previous week so nothing falls through)
                            ▼
                      Code checks ──fail──► skipped (bad schema, unknown label, likely duplicate)
                            │
                            ▼
                  Draft inserted (published = false, invisible to the public)
                            │
                            ▼
      Verification & validation agent (different model, fetches every source itself)
        C1 schema and sources   C2 every URL loads (checked by code, not the model)   C3 duplicate screen
        V1 sources reachable    V2 every claim supported   V3 dates   V4 evidence tier
        V5 classification       V6 taxonomy labels         V7 severity  V8 in scope and new   V9 neutral wording
                            │
          PASS ─────────────┼──── REVISE ──► corrections applied, re-verified once ──► PASS / REVISE
                            │                                                              │
                            ▼                                                              ▼
            published automatically if AUTO_PUBLISH=true               held for human review
            and tier is in AUTO_PUBLISH_TIERS; otherwise
            waits for your one-line approval                 REJECT ──► kept as a rejected draft
                            │
                            ▼
         GitHub issue with the week's report is opened for you
```

The database enforces the gate. A record can only be published if its most recent verification
is a PASS on the exact current version (a fingerprint of every field and source). Editing a
published record automatically unpublishes it until it is re-verified.

## One-time setup

1. **Supabase**: done. Project "AI AGENT BREACH Tracker" (`oqdinobmlldudutcvzjr`) has all four
   files in `sql/` applied, and `index.html` already points at it with the publishable key.
2. **GitHub repository**: create a repository and upload everything in this folder.
3. **Secrets** (Settings → Secrets and variables → Actions → Secrets):
   - `ANTHROPIC_API_KEY`: a Claude API key from the Claude Console (API usage is billed separately from a Claude.ai plan)
   - `SUPABASE_URL`: `https://oqdinobmlldudutcvzjr.supabase.co`
   - `SUPABASE_SERVICE_KEY`: the Supabase secret (service role) key. It stays in GitHub secrets and is never put in `index.html`.
4. **Variables** (same page → Variables), all optional:
   - `AUTO_PUBLISH`: `false` (default) or `true`
   - `AUTO_PUBLISH_TIERS`: default `T1,T2`
   - `RESEARCH_MODEL` (default `claude-sonnet-5`) and `VERIFY_MODEL` (default `claude-opus-5-5`). Keep them different so the verifier does not share the researcher's blind spots.
5. **Dashboard**: enable GitHub Pages (Settings → Pages → Deploy from a branch → `main` / root).
6. **First runs** (Actions → Weekly incident update → Run workflow):
   - `mode: weekly`, `dry_run: true` to see what it would do without writing anything.
   - `mode: backfill`, `limit: 10`: audits the 40 hand-curated records, 10 per run, and reports any
     it disagrees with. Legacy records stay published; findings come to you as an issue.

## Weekly review (about 10 minutes)

Open the GitHub issue the run creates. It lists records that passed, need review, or were rejected,
with the verifier's reasoning and any failed checks.

- Approve a passed record: `select public.pipeline_publish('AAB-2026-011');`
- Fix a record yourself: edit it in Table Editor → `tracker.incidents`, then trigger a manual run or record
  your own review: `select public.pipeline_record_verification('AAB-2026-011','PASS','human:Dr. K','Checked sources','[]');`
  (human reviews are logged with your name, and the fingerprint rule still applies).
- Full verifier detail for any record: `select * from tracker.verifications where incident_id = 'AAB-2026-011' order by run_at desc;`

## Costs and limits

Each weekly run makes one research call and roughly one or two verification calls per candidate,
all with web search and fetch. Check usage in the Claude Console after the first few runs.
GitHub Actions and Supabase free tiers are sufficient at this volume. GitHub pauses scheduled
workflows in repositories with no activity for 60 days; the weekly issues keep it active, but
check the Actions tab if an issue does not appear.
