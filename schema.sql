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
    severity           text,                        -- null for now; the future classifier fills this
    entities_affected  text,                        -- null for now; the future classifier fills this
    created_at         timestamptz not null default now()  -- when we inserted this row
);

-- Helpful indexes for later queries (newest-first listings, per-source filtering).
create index if not exists documents_published_date_idx on documents (published_date desc);
create index if not exists documents_source_idx on documents (source);

-- If you created the table BEFORE the `status` column existed, run this once to add it:
--   alter table documents add column if not exists status text not null default 'ok';
