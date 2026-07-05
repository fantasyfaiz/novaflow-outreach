-- Novaflow Outreach — contacts table
-- Run this in the Supabase SQL editor (or via `supabase db push`).

create table if not exists public.contacts (
  id             bigint generated always as identity primary key,
  first_name     text        not null default '',
  last_name      text        not null default '',
  email          text        not null,
  company        text        not null default '',
  job_title      text        not null default '',
  date_contacted timestamptz not null default now(),
  email_status   text        not null default 'sent',
  mode           text        not null default 'researcher',  -- 'researcher' | 'conference'
  follow_up_due  date,
  created_at     timestamptz not null default now()
);

-- One row per email address; re-sending updates the existing record.
-- (Emails are lowercased by the app before insert, so a plain unique index
--  gives case-insensitive dedupe and works with PostgREST on_conflict=email.)
create unique index if not exists contacts_email_key on public.contacts (email);

-- Row Level Security: on, with a policy that lets the anon key insert/update.
-- Tighten this later if you move to authenticated users.
alter table public.contacts enable row level security;

drop policy if exists "anon can upsert contacts" on public.contacts;
create policy "anon can upsert contacts"
  on public.contacts
  for all
  to anon
  using (true)
  with check (true);
