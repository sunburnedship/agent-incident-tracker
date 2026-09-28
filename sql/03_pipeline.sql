-- =====================================================================
-- AI Agent Breach Tracker — migration 03: weekly pipeline + verification gate
-- Run after 01_schema.sql and 02_seed.sql.
-- =====================================================================

-- ---------- Review status on incidents ----------
alter table tracker.incidents
  add column review_status text not null default 'draft'
  check (review_status in ('draft','passed','needs_review','rejected','legacy'));
update tracker.incidents set review_status = 'legacy' where published;   -- the manually curated seed

-- ---------- Content fingerprint: what exactly was verified ----------
-- Covers every substantive field plus the source list. Excludes bookkeeping
-- fields so that publishing or re-dating a row does not change the hash.
create or replace function tracker.record_hash(p_id text) returns text
language sql stable as $$
  select md5(
    (to_jsonb(i) - array['published','review_status','updated_at','date_added','last_verified'])::text
    || coalesce((select string_agg(s.citation || '|' || coalesce(s.url,''), '||' order by s.citation, s.url)
                 from tracker.sources s where s.incident_id = i.incident_id), '')
  )
  from tracker.incidents i where i.incident_id = p_id
$$;

-- ---------- Verification log ----------
create table tracker.verifications (
  verification_id  bigint generated always as identity primary key,
  incident_id      text not null references tracker.incidents(incident_id) on delete cascade,
  run_at           timestamptz not null default now(),
  verdict          text not null check (verdict in ('PASS','REVISE','REJECT')),
  reviewer         text not null,            -- model id, or 'human:<name>'
  record_hash      text not null,            -- fingerprint of the exact version reviewed
  summary          text,
  checks           jsonb not null default '[]'::jsonb,
  proposed_changes jsonb
);
create index on tracker.verifications (incident_id, run_at desc);

-- ---------- Pipeline run log ----------
create table tracker.pipeline_runs (
  run_id        bigint generated always as identity primary key,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  mode          text not null check (mode in ('weekly','backfill','manual')),
  window_start  date,
  window_end    date,
  candidates    int default 0,
  drafted       int default 0,
  passed        int default 0,
  needs_review  int default 0,
  rejected      int default 0,
  published     int default 0,
  report        text
);

alter table tracker.verifications enable row level security;
alter table tracker.pipeline_runs enable row level security;
-- No public policies: verification detail and run logs are internal.

-- ---------- Publish gate ----------
-- A record can go public only if the latest verification is a PASS on the
-- exact current version of the record (legacy seed rows are grandfathered).
create or replace function tracker.guard_publish() returns trigger
language plpgsql as $$
declare v record;
begin
  if new.published and (tg_op = 'INSERT' or not old.published) and new.review_status <> 'legacy' then
    select verdict, record_hash into v from tracker.verifications
      where incident_id = new.incident_id order by run_at desc, verification_id desc limit 1;
    if v is null or v.verdict <> 'PASS' then
      raise exception 'Cannot publish %: latest verification is %', new.incident_id, coalesce(v.verdict,'missing');
    end if;
    if v.record_hash <> tracker.record_hash(new.incident_id) then
      raise exception 'Cannot publish %: record changed after verification; re-verify first', new.incident_id;
    end if;
  end if;
  return new;
end $$;

create trigger incidents_guard_publish
before insert or update of published on tracker.incidents
for each row execute function tracker.guard_publish();

-- Editing a published, verified record pulls it back to review
create or replace function tracker.unpublish_on_edit() returns trigger
language plpgsql as $$
begin
  if old.published and new.published and old.review_status <> 'legacy'
     and (to_jsonb(new) - array['published','review_status','updated_at','last_verified'])
         <> (to_jsonb(old) - array['published','review_status','updated_at','last_verified']) then
    new.published := false;
    new.review_status := 'needs_review';
  end if;
  return new;
end $$;
create trigger incidents_unpublish_on_edit
before update on tracker.incidents
for each row execute function tracker.unpublish_on_edit();

-- ---------- Public view: add verification date ----------
-- verifications has RLS with no public policy; expose only PASS dates for
-- published records through a narrow security-definer function.
create or replace function public.agent_verified_dates()
returns table(incident_id text, verified_on date)
language sql stable security definer set search_path = '' as $$
  select v.incident_id, max(v.run_at)::date
  from tracker.verifications v
  join tracker.incidents i on i.incident_id = v.incident_id
  where v.verdict = 'PASS' and i.published and i.confidence_tier <> 'T4'
  group by v.incident_id
$$;
grant execute on function public.agent_verified_dates() to anon, authenticated;

drop view public.agent_incidents;
create view public.agent_incidents with (security_invoker = on) as
select i.*,
       extract(year from i.disclosure_date)::int as disclosure_year,
       case when i.incident_date_precision = 'day' and i.disclosure_date_precision = 'day'
            then i.disclosure_date - i.incident_date end as days_to_disclosure,
       coalesce((select json_agg(json_build_object('citation', s.citation, 'url', s.url, 'primary', s.is_primary)
                                 order by s.is_primary desc, s.source_id)
                 from tracker.sources s where s.incident_id = i.incident_id), '[]'::json) as sources,
       (select f.verified_on from public.agent_verified_dates() f where f.incident_id = i.incident_id) as verified_on
from tracker.incidents i;
grant select on public.agent_incidents to anon, authenticated;

-- =====================================================================
-- Pipeline RPCs — callable only with the service-role key (GitHub Actions)
-- =====================================================================

create or replace function public.pipeline_next_id(p_year int) returns text
language sql volatile security definer set search_path = '' as $$
  select format('AAB-%s-%s', p_year,
         lpad((coalesce(max(substring(incident_id from 10 for 3)::int), 0) + 1)::text, 3, '0'))
  from tracker.incidents where incident_id like 'AAB-' || p_year || '-%'
$$;

-- Insert a new draft (never published) with its sources; returns the assigned ID
create or replace function public.pipeline_insert_draft(rec jsonb, srcs jsonb)
returns text language plpgsql security definer set search_path = '' as $$
declare new_id text; r tracker.incidents;
begin
  new_id := public.pipeline_next_id(extract(year from (rec->>'disclosure_date')::date)::int);
  r := jsonb_populate_record(null::tracker.incidents,
         '{"entry_vector":[],"failure_mechanism":[],"impact":[],"owasp_llm":[],"owasp_agentic":[],"mitre_atlas":[]}'::jsonb
         || rec || jsonb_build_object('incident_id', new_id, 'published', false, 'review_status', 'draft',
                                   'date_added', current_date, 'updated_at', now()));
  insert into tracker.incidents select r.*;
  insert into tracker.sources (incident_id, citation, url, is_primary)
    select new_id, s->>'citation', nullif(s->>'url',''), coalesce((s->>'is_primary')::boolean, false)
    from jsonb_array_elements(srcs) s;
  return new_id;
end $$;

-- Apply a verifier's proposed corrections to a draft (and optionally replace sources)
create or replace function public.pipeline_apply_changes(p_id text, p_changes jsonb, p_srcs jsonb default null)
returns void language plpgsql security definer set search_path = '' as $$
declare cur tracker.incidents; nr tracker.incidents;
begin
  select * into cur from tracker.incidents where incident_id = p_id for update;
  if cur is null then raise exception 'No incident %', p_id; end if;
  nr := jsonb_populate_record(cur, p_changes - array['incident_id','published','review_status','date_added']);
  update tracker.incidents set
    title=nr.title, summary=nr.summary, event_class=nr.event_class, scope_fit=nr.scope_fit,
    confidence_tier=nr.confidence_tier, incident_date=nr.incident_date, incident_date_precision=nr.incident_date_precision,
    disclosure_date=nr.disclosure_date, disclosure_date_precision=nr.disclosure_date_precision,
    entry_vector=nr.entry_vector, failure_mechanism=nr.failure_mechanism, impact=nr.impact,
    owasp_llm=nr.owasp_llm, owasp_agentic=nr.owasp_agentic, mitre_atlas=nr.mitre_atlas,
    adversary_involved=nr.adversary_involved, agent_config=nr.agent_config, vendor_model=nr.vendor_model,
    product_framework=nr.product_framework, sector=nr.sector, autonomy_level=nr.autonomy_level,
    human_in_loop=nr.human_in_loop, severity_realized=nr.severity_realized, severity_potential=nr.severity_potential,
    records_affected=nr.records_affected, users_affected=nr.users_affected, orgs_affected=nr.orgs_affected,
    financial_loss_usd=nr.financial_loss_usd, downtime_hours=nr.downtime_hours,
    remediation_status=nr.remediation_status, notes=nr.notes
  where incident_id = p_id;
  if p_srcs is not null then
    delete from tracker.sources where incident_id = p_id;
    insert into tracker.sources (incident_id, citation, url, is_primary)
      select p_id, s->>'citation', nullif(s->>'url',''), coalesce((s->>'is_primary')::boolean, false)
      from jsonb_array_elements(p_srcs) s;
  end if;
end $$;

-- Record a verdict against the record's current fingerprint and set review status
create or replace function public.pipeline_record_verification(
  p_id text, p_verdict text, p_reviewer text, p_summary text, p_checks jsonb, p_changes jsonb default null)
returns bigint language plpgsql security definer set search_path = '' as $$
declare vid bigint;
begin
  insert into tracker.verifications (incident_id, verdict, reviewer, record_hash, summary, checks, proposed_changes)
  values (p_id, p_verdict, p_reviewer, tracker.record_hash(p_id), p_summary, coalesce(p_checks,'[]'::jsonb), p_changes)
  returning verification_id into vid;
  update tracker.incidents
     set review_status = case when review_status = 'legacy' then 'legacy'
                              when p_verdict = 'PASS' then 'passed'
                              when p_verdict = 'REVISE' then 'needs_review'
                              else 'rejected' end,
         last_verified = case when p_verdict = 'PASS' then current_date else last_verified end
   where incident_id = p_id;
  return vid;
end $$;

create or replace function public.pipeline_publish(p_id text) returns void
language sql security definer set search_path = '' as $$
  update tracker.incidents set published = true where incident_id = p_id
$$;

create or replace function public.pipeline_log_run(p jsonb) returns bigint
language sql security definer set search_path = '' as $$
  insert into tracker.pipeline_runs (started_at, finished_at, mode, window_start, window_end,
         candidates, drafted, passed, needs_review, rejected, published, report)
  values ((p->>'started_at')::timestamptz, now(), p->>'mode', (p->>'window_start')::date, (p->>'window_end')::date,
          (p->>'candidates')::int, (p->>'drafted')::int, (p->>'passed')::int, (p->>'needs_review')::int,
          (p->>'rejected')::int, (p->>'published')::int, p->>'report')
  returning run_id
$$;

-- All records (drafts included) for de-duplication and backfill — service role only
create or replace function public.pipeline_all_records() returns setof json
language sql stable security definer set search_path = '' as $$
  select row_to_json(x) from (
    select i.*, coalesce((select json_agg(json_build_object('citation',s.citation,'url',s.url,'is_primary',s.is_primary))
                          from tracker.sources s where s.incident_id = i.incident_id), '[]'::json) as sources,
           (select count(*) from tracker.verifications v where v.incident_id = i.incident_id) as n_verifications
    from tracker.incidents i order by i.incident_id) x
$$;

do $$
declare f text;
begin
  foreach f in array array[
    'public.pipeline_next_id(int)','public.pipeline_insert_draft(jsonb,jsonb)',
    'public.pipeline_apply_changes(text,jsonb,jsonb)',
    'public.pipeline_record_verification(text,text,text,text,jsonb,jsonb)',
    'public.pipeline_publish(text)','public.pipeline_log_run(jsonb)','public.pipeline_all_records()']
  loop
    execute format('revoke all on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
end $$;
