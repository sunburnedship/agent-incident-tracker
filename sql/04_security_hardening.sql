-- Applied 2026-09-28 after running the Supabase security advisor.
alter function tracker.record_hash(text) set search_path = '';
alter function tracker.guard_publish() set search_path = '';
alter function tracker.unpublish_on_edit() set search_path = '';
alter function tracker.validate_labels() set search_path = '';

create or replace function tracker.verified_dates()
returns table(incident_id text, verified_on date)
language sql stable security definer set search_path = '' as $$
  select v.incident_id, max(v.run_at)::date
  from tracker.verifications v
  join tracker.incidents i on i.incident_id = v.incident_id
  where v.verdict = 'PASS' and i.published and i.confidence_tier <> 'T4'
  group by v.incident_id
$$;
revoke all on function tracker.verified_dates() from public;
grant execute on function tracker.verified_dates() to anon, authenticated, service_role;

create or replace view public.agent_incidents with (security_invoker = on) as
select i.*,
       extract(year from i.disclosure_date)::int as disclosure_year,
       case when i.incident_date_precision = 'day' and i.disclosure_date_precision = 'day'
            then i.disclosure_date - i.incident_date end as days_to_disclosure,
       coalesce((select json_agg(json_build_object('citation', s.citation, 'url', s.url, 'primary', s.is_primary)
                                 order by s.is_primary desc, s.source_id)
                 from tracker.sources s where s.incident_id = i.incident_id), '[]'::json) as sources,
       (select f.verified_on from tracker.verified_dates() f where f.incident_id = i.incident_id) as verified_on
from tracker.incidents i;

drop function public.agent_verified_dates();
