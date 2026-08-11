# job-search-agents

A job search runs on two things that don't mix well: a wide net, and a strict filter. This
repo is the filter — four specialist agents plus a sourcing pipeline that only surfaces roles
which were *genuinely* posted in the last few days, are open right now, meet the level you
claim to be, and that you haven't already applied to.

The submit button stays human. Everything before it is automated.

```
1200+ employer ATS boards                  ~80,000 live postings
        │
        ▼
  first-publish date only ────────────────  drops reposts pretending to be new
  title / level / location ───────────────  drops senior, internships, non-US
  JD body: years bar, ITAR, visa ─────────  drops what the title hid
  your mailbox, last 15 days ─────────────  drops companies you already applied to
        │
        ▼
     a handful of roles ──► job-vetter ──► résumé + outreach ──► you apply
```

On a representative run that funnel went 79,504 → 8.

## Why the freshness part is not trivial

Applying inside the first day or two of a posting matters more than almost anything else in
the pipeline, which makes "when was this posted" the load-bearing field. Every convenient
source lies about it.

Aggregators report their own crawl date, so a req can read "reposted 2 days ago" years after
it opened. Worse, the obvious field on the employer's own API is wrong too — Greenhouse's
`updated_at` bumps on any edit:

```bash
curl -s https://boards-api.greenhouse.io/v1/boards/verisign/jobs/7766899003 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['updated_at'], d['first_published'])"
# 2026-08-06  ...  2026-07-01
```

Four days old by the field most tools read. Forty-one days old in reality. This pipeline
reads `first_published` on Greenhouse, `createdAt` on Lever, `publishedAt` on Ashby, and
**drops any posting with no first-publish field at all** rather than guessing.

## Why the title is not the level

From one 27-role sample, all titled some variant of "Software Engineer":

| Title as posted | What the body required |
|---|---|
| Fullstack Software Engineer | "typically 6+ years" plus technical leadership |
| Software Engineer, Desktop | "5+ years", plus Rust/Swift/C++ |
| Software Engineer (Perception) | "10+ years ... Staff or Principal level" |
| Software Developer | US citizenship, Top Secret/SCI, 8 years |
| Software Engineer - Python | "An active SECRET security clearance is required." |

None of that is visible from a title, a location or a salary band. So after the cheap
filters narrow the field, the pipeline fetches the description for the survivors only and
reads the years bar, the clearance and ITAR language, and any explicit visa-category
exclusion. One fetch per survivor, not per posting scanned.

The years check takes the *minimum* across all mentions on purpose. A posting asking for
"3+ years backend, 1+ year Python" really wants one, and quietly hiding an entry-level
req is a worse failure than showing one extra row.

## The duplicate check

Applying twice to the same company reads as spam, and several applicant-tracking systems
now auto-reject on it. The check that catches it has one non-obvious requirement:
**confirmations come from the tracking system, not the employer.** A search for
`from:company.com` will not see `something@myworkday.com`, which is exactly how a duplicate
gets through. The sender list here is ATS-first, and the message listing pages to the end,
because a truncated result produces a false "not applied" — the one failure the check
exists to prevent.

## The agents

Definitions live in `agents/` and drop into a coding-agent CLI that supports subagents
(copy to `.claude/agents/`). They read your details from `profile.yml` rather than having
them baked in, which is what allows them to be public.

| Agent | What it does |
|---|---|
| `job-vetter` | Eligibility first, then real level, lane, live-check, send format, résumé variant, keywords to mirror, and any claim you can't yet defend |
| `outreach-writer` | Cold email, referral ask, informational-interview request. Refuses to write mail-merge without a specific hook |
| `profile-optimizer` | Headline, featured items, repo pins, and build-in-public post drafts |
| `proof-of-work` | Designs one small real artifact for a target company, plus the message that delivers it to a human |

Two conventions run through all four. **Nothing gets claimed that your profile doesn't
list** — a missing skill is reported as an honest gap for the cover letter, never invented
onto a résumé. And **anything marked `defensible: false` is flagged rather than featured**,
because a project you can't explain end to end is worse than one you never mentioned.

## Setup

```bash
cp profile.example.yml profile.yml            # your details; gitignored
cp pipeline/companies.example.txt pipeline/companies.txt

python3 pipeline/discover_boards.py           # dry run: harvest boards from public feeds
python3 pipeline/discover_boards.py --write   # append the verified ones

python3 pipeline/fresh.py                     # writes pipeline/FRESH.md
python3 pipeline/fresh.py --days 7 --no-mail
```

The duplicate check needs a stored OAuth refresh token:

```bash
export MAIL_CREDENTIALS=~/path/credentials.json   # holds refresh_token
export MAIL_OAUTH_KEYS=~/path/oauth_client.json   # holds client_id / client_secret
```

Read-only. No dependencies outside the standard library.

## What this deliberately does not do

It does not submit applications. Full auto-apply tools produce the worst outcomes of any
category — interview rates in the low single digits, terms-of-service violations, silent
submit failures, and duplicate spam that burns the companies you cared about. Employer-side
countermeasures have hardened accordingly: per-candidate application caps with auto-reject,
re-apply cooldowns, identity verification, honeypot fields in forms.

It also does not touch a logged-in session on any job board. Everything here reads public,
unauthenticated APIs — the same endpoints the employer's own careers page calls.

And it does not tell you a role is a *good* role. The pipeline reads titles, dates and
requirement text; it cannot tell whether the work is interesting or the team is any good.
It answers exactly one question — what genuinely appeared in the last few days that I'm
eligible for and haven't already applied to — and leaves the judgment to the vetter and to
you.

## License

MIT
