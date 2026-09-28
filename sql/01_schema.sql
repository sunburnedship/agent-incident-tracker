-- =====================================================================
-- AI Agent Breach Tracker — schema v1
-- Postgres / Supabase. Run once in a fresh project (SQL editor or migration).
-- =====================================================================

create schema if not exists tracker;

-- ---------- Controlled vocabulary (multi-label fields) ----------
create table tracker.codebook (
  stage          text not null check (stage in ('entry_vector','failure_mechanism','impact')),
  label          text not null,
  definition     text not null,
  owasp_llm      text,
  owasp_agentic  text,
  mitre_atlas    text,
  primary key (stage, label)
);

-- ---------- Incidents ----------
create table tracker.incidents (
  incident_id                text primary key check (incident_id ~ '^AAB-[0-9]{4}-[0-9]{3}$'),
  title                      text not null,
  summary                    text not null,
  event_class                text not null check (event_class in ('Realized incident','Controlled-eval incident','Demonstrated vulnerability')),
  scope_fit                  text not null check (scope_fit in ('Core','Adjacent')),
  confidence_tier            text not null check (confidence_tier in ('T1','T2','T3','T4')),
  incident_date              date,
  incident_date_precision    text check (incident_date_precision in ('day','month','year')),
  disclosure_date            date not null,
  disclosure_date_precision  text not null check (disclosure_date_precision in ('day','month','year')),
  entry_vector               text[] not null default '{}',
  failure_mechanism          text[] not null default '{}',
  impact                     text[] not null default '{}',
  owasp_llm                  text[] not null default '{}',
  owasp_agentic              text[] not null default '{}',
  mitre_atlas                text[] not null default '{}',
  adversary_involved         text not null check (adversary_involved in ('Yes','No','Unknown')),
  agent_config               text not null check (agent_config in ('Single agent','Multi-agent','Multi-agent/swarm','N/A')),
  vendor_model               text,
  product_framework          text,
  sector                     text,
  autonomy_level             text check (autonomy_level in ('L1 Assistive','L2 Tool-using w/ approval','L3 Autonomous tool use','L4 Long-horizon / orchestration','N/A')),
  human_in_loop              text check (human_in_loop in ('Yes','No','Partial','Unknown','N/A')),
  severity_realized          smallint not null check (severity_realized between 1 and 5),
  severity_potential         smallint check (severity_potential between 1 and 5),
  records_affected           bigint check (records_affected >= 0),
  users_affected             bigint check (users_affected >= 0),
  orgs_affected              integer check (orgs_affected >= 0),
  financial_loss_usd         numeric(16,2) check (financial_loss_usd >= 0),
  downtime_hours             numeric(10,2) check (downtime_hours >= 0),
  remediation_status         text,
  notes                      text,
  published                  boolean not null default false,   -- review gate: drafts stay private
  date_added                 date not null default current_date,
  last_verified              date,
  updated_at                 timestamptz not null default now(),
  check (incident_date is null or incident_date <= disclosure_date)
);

-- Multi-label fields must use exact codebook labels
create or replace function tracker.validate_labels() returns trigger
language plpgsql as $$
declare bad text;
begin
  select string_agg(format('%s: "%s"', s.stage, s.lbl), '; ') into bad
  from (
    select 'entry_vector' as stage, unnest(new.entry_vector) as lbl
    union all select 'failure_mechanism', unnest(new.failure_mechanism)
    union all select 'impact', unnest(new.impact)
  ) s
  where not exists (
    select 1 from tracker.codebook c
    where c.stage = s.stage
      and (c.label = s.lbl or s.lbl = c.label || ' (attempted)')
  );
  if bad is not null then
    raise exception 'Unknown codebook label(s) on %: %', new.incident_id, bad;
  end if;
  new.updated_at := now();
  return new;
end $$;

create trigger incidents_validate
before insert or update on tracker.incidents
for each row execute function tracker.validate_labels();

-- ---------- Sources ----------
create table tracker.sources (
  source_id    bigint generated always as identity primary key,
  incident_id  text not null references tracker.incidents(incident_id) on delete cascade,
  citation     text not null,
  url          text check (url is null or url ~ '^https?://'),
  is_primary   boolean not null default false
);
create index on tracker.sources (incident_id);

-- ---------- Change log ----------
create table tracker.changelog (
  change_id    bigint generated always as identity primary key,
  changed_on   date not null default current_date,
  change       text not null,
  records      text,
  author       text
);

-- ---------- Public read layer ----------
alter table tracker.codebook  enable row level security;
alter table tracker.incidents enable row level security;
alter table tracker.sources   enable row level security;
alter table tracker.changelog enable row level security;

create policy public_read_codebook on tracker.codebook for select to anon, authenticated using (true);
create policy public_read_incidents on tracker.incidents for select to anon, authenticated
  using (published and confidence_tier <> 'T4');
create policy public_read_sources on tracker.sources for select to anon, authenticated
  using (exists (select 1 from tracker.incidents i where i.incident_id = sources.incident_id
                 and i.published and i.confidence_tier <> 'T4'));
create policy public_read_changelog on tracker.changelog for select to anon, authenticated using (true);
-- No insert/update/delete policies: anon and authenticated users cannot write.
-- Writes happen through the Supabase dashboard, SQL editor, or the service role.

-- Flat view the dashboard reads (respects RLS via security_invoker)
create view public.agent_incidents with (security_invoker = on) as
select i.*,
       extract(year from i.disclosure_date)::int as disclosure_year,
       case when i.incident_date_precision = 'day' and i.disclosure_date_precision = 'day'
            then i.disclosure_date - i.incident_date end as days_to_disclosure,
       coalesce((select json_agg(json_build_object('citation', s.citation, 'url', s.url, 'primary', s.is_primary)
                                 order by s.is_primary desc, s.source_id)
                 from tracker.sources s where s.incident_id = i.incident_id), '[]'::json) as sources
from tracker.incidents i;

create view public.agent_codebook with (security_invoker = on) as
select * from tracker.codebook;

create view public.agent_changelog with (security_invoker = on) as
select * from tracker.changelog order by changed_on desc, change_id desc;

grant usage on schema tracker to anon, authenticated;
grant select on tracker.codebook, tracker.incidents, tracker.sources, tracker.changelog to anon, authenticated;
grant select on public.agent_incidents, public.agent_codebook, public.agent_changelog to anon, authenticated;
