# AI Agent Incident Tracker

A public dashboard (`index.html` on GitHub Pages) reading a Supabase database, kept up to date by
two Claude Cowork scheduled tasks: one researches new incidents, and a separate one verifies them.

```
Monday 6:00 a.m.  Cowork task 1: Research
                  Searches the last 10 days of disclosures, screens out duplicates in the
                  database, saves new incidents as unpublished drafts.
                        │
Monday 9:00 a.m.  Cowork task 2: Verification & validation (a fresh session)
                  Sees only the drafts, never the researcher's reasoning. Fetches every source
                  and runs nine checks: sources, claims, dates, evidence tier, classification,
                  taxonomy, severity, scope/duplicates, neutral wording.
                        │
          PASS ─────────┼──── REVISE: corrects fields it can prove, re-checks once
                        │     REJECT: kept as a rejected draft, never shown
                        ▼
      Waits for your approval (or publishes itself if auto_publish is on)
                        │
                        ▼
      Dashboard shows it on the next page load
```

The safeguards live in the database, so they hold no matter who writes to it:
category labels must match the codebook, a record can only be published after a PASS on its
exact current version, and editing a published record takes it offline until it is re-verified.

## What's in this folder

| Item | Purpose |
|---|---|
| `index.html` | The dashboard. Already connected to the database. |
| `cowork-tasks/1-weekly-research.md` | Instructions for the Monday research task |
| `cowork-tasks/2-weekly-verification.md` | Instructions for the Monday verification task |
| `cowork-tasks/3-backfill-audit.md` | One-off audit of the 40 original records |
| `sql/` | Database history (already applied to your Supabase project; kept for the record) |

## Setup

### 1. Database: done
Supabase project "AI AGENT BREACH Tracker" (`oqdinobmlldudutcvzjr`) has all five SQL files applied.

### 2. Dashboard on GitHub Pages
1. Upload the contents of this folder to your GitHub repository (`index.html` must be at the top level).
   If you already uploaded the earlier version, delete the `.github` and `pipeline` folders and
   `requirements.txt`. No GitHub secrets are needed; delete any you created.
2. Settings → Pages → Deploy from a branch → `main`, `/ (root)` → Save.
3. Open the site address GitHub shows. The line under the title should read
   "Live from the tracker database."

### 3. Cowork scheduled tasks
In Claude Desktop, open Cowork and make sure the Supabase connector is connected there.

For each of the two weekly task files:
1. Click **Scheduled** in the left sidebar, then create a new task.
2. Paste the entire contents of the task file as the instructions.
3. Set it to repeat weekly on Monday: 6:00 a.m. for research, 9:00 a.m. for verification.
4. Save.

Run each once by hand to test: research first, then verification after it finishes.
Read both reports before trusting the schedule.

### 4. Audit the original 40 records
Create a third task from `3-backfill-audit.md` with no repeating schedule (or paste it into a
regular Cowork task). Run it four times; each run audits the next 10 records. The records stay
published; the reports list any corrections the verifier recommends.

## Weekly review (about 10 minutes)

Open the verification task's latest run in Cowork's Scheduled page and read the report.

- **Approve a passed record:** in Supabase, open SQL Editor and run
  `select public.pipeline_publish('AAB-2026-011');`
  Or ask Claude in the AI AGENT BREACHES project to publish it.
- **A record that needs review:** the report says what is wrong. Fix it in Table Editor
  (schema `tracker` → `incidents`), then the next verification run re-checks it automatically,
  because it changed since its last review.
- **See every verdict and check for a record:**
  `select * from tracker.verifications where incident_id = 'AAB-2026-011' order by run_at desc;`
- **See run history:** `select mode, started_at, candidates, passed, needs_review, rejected, published from tracker.pipeline_runs order by started_at desc;`

## Settings (Table Editor → schema `tracker` → `settings`)

| Key | Default | Meaning |
|---|---|---|
| `auto_publish` | `false` | Set to `true` to let the verification task publish PASS records itself |
| `auto_publish_tiers` | `T1,T2` | Tiers eligible for automatic publishing |
| `research_window_days` | `10` | How far back each weekly search looks (7 days plus overlap) |

Changing a setting takes effect on the next run; no task instructions need editing.

## Good to know
- Scheduled tasks run remotely on their schedule, even with your computer asleep.
- Each run uses your Claude plan's usage. Research with many searches is the heavier task;
  check your usage after the first few weeks.
- Both tasks are instructed to use only the tracker's pipeline functions. They sign in to Supabase
  as you, so that rule is enforced by their instructions; the publishing gate itself is enforced
  by the database.
