-- PolicyPulse — database schema
-- Run this in the Supabase SQL Editor (Supabase dashboard -> SQL Editor -> New query).

create table if not exists documents (
    id                 bigint generated always as identity primary key,
    source             text not null,              -- which crawler produced this, e.g. 'rbi_notifications'
    title              text,                        -- notification title
    published_date     date,                        -- date the regulator published it
    url                text,                        -- link to the detail / listing page
    pdf_url            text,                        -- link to the PDF we downloaded
    full_text          text,                        -- text we extracted from the PDF
    content_hash       text unique,                 -- fingerprint for de-dup: sha256 of full_text (ok rows) or of url (needs_review rows)
    status             text not null default 'ok',  -- 'ok' = text extracted; 'needs_review' = no PDF or text couldn't be extracted
    severity           text,                        -- 'high'/'medium'/'low'; the classifier fills this
    entities_affected  text,                        -- who's affected (e.g. 'NBFCs, Banks'); the classifier fills this
    summary            text,                        -- 1-2 sentence AI summary; the classifier fills this
    created_at         timestamptz not null default now()  -- when we inserted this row
);

-- Helpful indexes for later queries (newest-first listings, per-source filtering).
create index if not exists documents_published_date_idx on documents (published_date desc);
create index if not exists documents_source_idx on documents (source);

-- If you created the table BEFORE the `status` column existed, run this once to add it:
--   alter table documents add column if not exists status text not null default 'ok';

-- If you created the table BEFORE the `summary` column existed, run this once to add it:
--   alter table documents add column if not exists summary text;


-- ======================================================================================
-- subscribers — the email list for the daily brief.
-- OPTIONAL for now: you only need to run this when you're ready to start collecting
-- subscribers (Day 6). The distribution/send_brief.py script can preview without it
-- (using --test-recipient).
-- ======================================================================================

create table if not exists subscribers (
    id                 bigint generated always as identity primary key,
    email              text unique not null,          -- the subscriber's email address
    name               text,                           -- optional display name
    active             boolean not null default true,  -- set to false once they unsubscribe
    unsubscribe_token  uuid not null default gen_random_uuid(),  -- unique per subscriber; used in the unsubscribe link
    subscribed_at      timestamptz not null default now(),
    unsubscribed_at    timestamptz                     -- when they unsubscribed (null while active)
);
