-- Applied 2026-09-28. Support for Claude Cowork scheduled tasks.
alter table tracker.pipeline_runs drop constraint pipeline_runs_mode_check;
alter table tracker.pipeline_runs add constraint pipeline_runs_mode_check
  check (mode in ('weekly','backfill','manual','research','verify'));

create table tracker.settings (key text primary key, value text not null, description text);
alter table tracker.settings enable row level security;
insert into tracker.settings (key, value, description) values
  ('auto_publish', 'false', 'true = the verification task publishes PASS records whose tier is listed in auto_publish_tiers'),
  ('auto_publish_tiers', 'T1,T2', 'Comma-separated tiers eligible for automatic publishing'),
  ('research_window_days', '10', 'How many days back the research task searches (7-day cadence + overlap)');

create extension if not exists pg_trgm with schema extensions;

create or replace function public.pipeline_index()
returns table(incident_id text, disclosure_date date, title text, vendor_model text,
              review_status text, published boolean, urls text[])
language sql stable security definer set search_path = '' as $$
  select i.incident_id, i.disclosure_date, i.title, i.vendor_model, i.review_status, i.published,
         coalesce(array(select s.url from tracker.sources s where s.incident_id = i.incident_id and s.url is not null), '{}')
  from tracker.incidents i order by i.disclosure_date desc
$$;

create or replace function public.pipeline_find_similar(p_title text, p_urls text[] default '{}', p_exclude text default null)
returns table(incident_id text, title text, disclosure_date date, title_similarity real, shared_urls text[])
language sql stable security definer set search_path = '' as $$
  select i.incident_id, i.title, i.disclosure_date,
         extensions.similarity(lower(i.title), lower(p_title)) as title_similarity,
         coalesce(array(select s.url from tracker.sources s
                        where s.incident_id = i.incident_id and s.url = any(p_urls)), '{}') as shared_urls
  from tracker.incidents i
  where (p_exclude is null or i.incident_id <> p_exclude)
    and (extensions.similarity(lower(i.title), lower(p_title)) >= 0.35
         or exists (select 1 from tracker.sources s where s.incident_id = i.incident_id and s.url = any(p_urls)))
  order by 4 desc
  limit 10
$$;

create or replace function public.pipeline_pending_review(p_backfill boolean default false, p_limit int default 25)
returns setof json
language sql stable security definer set search_path = '' as $$
  select row_to_json(x) from (
    select i.*,
           coalesce((select json_agg(json_build_object('citation',s.citation,'url',s.url,'is_primary',s.is_primary))
                     from tracker.sources s where s.incident_id = i.incident_id), '[]'::json) as sources
    from tracker.incidents i
    where case when p_backfill then
                 i.review_status = 'legacy'
                 and not exists (select 1 from tracker.verifications v where v.incident_id = i.incident_id)
               else
                 i.review_status in ('draft','needs_review')
                 and coalesce((select v.record_hash from tracker.verifications v where v.incident_id = i.incident_id
                               order by v.run_at desc, v.verification_id desc limit 1), '')
                     <> tracker.record_hash(i.incident_id)
          end
    order by i.incident_id
    limit p_limit) x
$$;

create or replace function public.pipeline_settings()
returns table(key text, value text)
language sql stable security definer set search_path = '' as $$
  select key, value from tracker.settings
$$;

do $$
declare f text;
begin
  foreach f in array array[
    'public.pipeline_index()','public.pipeline_find_similar(text,text[],text)',
    'public.pipeline_pending_review(boolean,int)','public.pipeline_settings()']
  loop
    execute format('revoke all on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
end $$;
