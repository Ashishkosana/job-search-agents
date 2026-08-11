#!/usr/bin/env python3
"""Find software-engineering roles that were genuinely posted in the last N days.

Job boards are full of postings that look new and are not. Three specific lies, and
what this does about each:

1. "Reposted 2 days ago" on an aggregator is the aggregator's crawl date.
   Greenhouse's own `updated_at` is no better: it bumps on every edit, so a req first
   published 41 days ago reads as 4 days old. This reads the employer's FIRST-publish
   field only (`first_published` / `createdAt` / `publishedAt`) and drops any posting
   that has no such field rather than guessing.

2. The title lies about level. Across one 27-role sample, postings titled plainly
   "Software Engineer" turned out to require 5, 6 and 10 years. So after the cheap
   filters, this fetches the JD body for the survivors and reads the actual years bar.

3. The eligibility bar is in the body too. Clearance, ITAR and US-person requirements
   rarely appear in the title, and neither does an explicit exclusion of a particular
   work-authorization category by name. Both are read from the body.

Then, optionally, it checks your own mail for an application confirmation so you do not
apply to the same company twice. Confirmations come from the ATS
(`something@myworkday.com`), never from `careers@company.com`, which is why the sender
list below is ATS-first -- searching by company domain silently misses them.

    python3 fresh.py                      # last 3 days, mail check on
    python3 fresh.py --days 7             # wider window
    python3 fresh.py --no-mail            # skip the duplicate check

Configure the boards to poll in companies.txt (see companies.example.txt).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).parent
COMPANIES = HERE / "companies.txt"
OUT = HERE / "FRESH.md"
NOW = datetime.now(timezone.utc)

# --- what counts as the role you want -----------------------------------------------
ROLE = re.compile(
    r"\b(software\s+engineer|software\s+developer|software\s+development\s+engineer|"
    r"back[\s-]?end\s+engineer|front[\s-]?end\s+engineer|full[\s-]?stack\s+(engineer|developer)|"
    r"web\s+developer|application\s+developer|platform\s+engineer|infrastructure\s+engineer|"
    r"programmer|swe|sde)\b", re.I)
SENIOR = re.compile(
    r"(\b(senior|staff|principal|lead|manager|director|head\s+of|vp|architect|"
    r"II|III|IV|L[4-9])\b|\bsr\b)", re.I)
NEWGRAD = re.compile(
    r"\b(new\s?grad|graduate|university\s?grad|campus|early\s?career|entry[\s-]?level|"
    r"college\s+grad(uate)?)\b", re.I)
JUNIOR = re.compile(
    r"\b(junior|jr\.?|associate\s+(software|engineer)|software\s+engineer\s+[i1]\b|"
    r"engineer\s+[i1]\b|sde\s?(1|i)\b|level\s?1|entry)\b", re.I)
INTERNSHIP = re.compile(r"\b(intern|internship|co[\s-]?op|summer\s+20\d\d)\b", re.I)
NON_US = re.compile(
    r"\b(india|united kingdom|london|canada|toronto|germany|berlin|poland|remote emea|"
    r"singapore|australia|brazil|mexico|japan|tokyo|israel|tel aviv|dublin|ireland|"
    r"netherlands|amsterdam|france|paris|spain|portugal|lisbon)\b", re.I)

# --- eligibility gates, read from the JD body ---------------------------------------
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|to|-|–)?\s*(?:\d{1,2})?\s*\+?\s*years?\b", re.I)
INELIGIBLE = re.compile(
    r"(itar\b|international traffic in arms|\bu\.?s\.? person\b|export control|"
    r"security clearance|\btop[\s-]?secret\b|\bts/sci\b|active secret|"
    r"must be a u\.?s\.? citizen|u\.?s\.? citizenship (?:is )?required)", re.I)
# An explicit visa-category exclusion is not the same as "we don't sponsor": the first
# bars a candidate outright, the second may still be worth applying to. Set this to the
# category names that apply to you; leave empty to disable the check.
VISA_CATEGORIES = ("OPT", "CPT", "H-1B", "TN")
_CAT = "|".join(re.escape(c) for c in VISA_CATEGORIES)
EXCLUDES_VISA = re.compile(
    rf"(not\s+(?:currently\s+)?(?:be\s+)?able to (?:engage|hire|consider)[^.]{{0,40}}\b({_CAT})\b|"
    rf"cannot[^.]{{0,30}}\b({_CAT})\b|\bno\b[^.]{{0,20}}\b({_CAT})\b)", re.I) \
    if VISA_CATEGORIES else re.compile(r"(?!)")

# Employers and facilities where the work is cleared regardless of what the title says.
# Extend for your own no-go list.
BLOCKED_EMPLOYERS = {
    "lockheed martin", "raytheon", "rtx", "northrop grumman", "general dynamics", "gdit",
    "booz allen", "saic", "leidos", "l3harris", "bae systems", "caci", "peraton", "mantech",
    "mitre", "parsons", "anduril", "spacex", "varda", "defense unicorns", "captivation",
}
CLEARED_SITES = re.compile(
    r"(annapolis junction|fort meade|ft\.? meade|\bafb\b|air force base|quantico|langley|"
    r"redstone|aberdeen proving|wright[\s-]patterson|patuxent)", re.I)

# Application confirmations arrive from the applicant-tracking system, not the employer.
ATS_SENDERS = (
    "myworkday.com", "myworkdaysite.com", "greenhouse-mail.io", "ashbyhq.com",
    "hire.lever.co", "lever.co", "bamboohr.com", "icims.com", "successfactors.com",
    "smartrecruiters.com", "workable.com", "jobvite.com", "taleo.net", "brassring.com",
    "oraclecloud.com", "avature.net",
)
APPLIED_SUBJECTS = ("applying", "application", "applied", "we received", "received your")
CORP_SUFFIXES = (
    " incorporated", " inc", " llc", " ltd", " limited", " corporation", " corp", " company",
    " co", " technologies", " technology", " software", " systems", " solutions", " labs",
    " group", " holdings", " ai", " io", " hq",
)


def get(url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": "fresh-jobs/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):  # Lever uses epoch milliseconds
        return datetime.fromtimestamp(v / 1000, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# --- sources -------------------------------------------------------------------------


def _row(company: str, title: str, loc: str, url: str, published: datetime | None,
         source: str, slug: str, jid: Any) -> dict[str, Any]:
    return {"company": company, "title": title, "loc": loc, "url": url,
            "published": published, "source": source, "slug": slug, "jid": str(jid)}


def greenhouse(slug: str, name: str) -> list[dict[str, Any]]:
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false")
    return [_row(name, j["title"], (j.get("location") or {}).get("name", ""),
                 j["absolute_url"], parse_dt(j.get("first_published")),
                 "greenhouse", slug, j["id"])
            for j in d.get("jobs", [])]


def lever(slug: str, name: str) -> list[dict[str, Any]]:
    d = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [_row(name, j["text"], (j.get("categories") or {}).get("location", ""),
                 j["hostedUrl"], parse_dt(j.get("createdAt")), "lever", slug, j.get("id", ""))
            for j in d]


def ashby(slug: str, name: str) -> list[dict[str, Any]]:
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [_row(name, j["title"], j.get("location", ""), j["jobUrl"],
                 parse_dt(j.get("publishedAt")), "ashby", slug, j.get("id", ""))
            for j in d.get("jobs", []) if j.get("isListed") is not False]


FETCHERS: dict[str, Callable[[str, str], list[dict[str, Any]]]] = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def body_of(row: dict[str, Any]) -> str:
    """The posting's text. One fetch per survivor, not per posting scanned."""
    try:
        if row["source"] == "greenhouse":
            return get("https://boards-api.greenhouse.io/v1/boards/"
                       f"{row['slug']}/jobs/{row['jid']}").get("content", "")
        if row["source"] == "lever":
            for j in get(f"https://api.lever.co/v0/postings/{row['slug']}?mode=json"):
                if str(j.get("id")) == row["jid"]:
                    return j.get("descriptionPlain", "") + " " + str(j.get("lists", ""))
        if row["source"] == "ashby":
            d = get(f"https://api.ashbyhq.com/posting-api/job-board/{row['slug']}")
            for j in d.get("jobs", []):
                if str(j.get("id")) == row["jid"]:
                    return j.get("descriptionPlain", "")
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return ""
    return ""


def years_floor(body: str) -> int | None:
    """Lowest number of years the posting asks for, or None if it never says.

    Deliberately the minimum across all mentions: a JD asking "3+ years backend,
    1+ year Python" really wants 1, and erring toward keeping beats silently hiding
    an entry-level req.
    """
    found = [int(m.group(1)) for m in YEARS.finditer(body) if int(m.group(1)) <= 20]
    return min(found) if found else None


# --- duplicate-application check ----------------------------------------------------


def mail_token() -> str:
    """Access token from a stored OAuth refresh token.

    Point MAIL_CREDENTIALS at a JSON file holding `refresh_token`, and MAIL_OAUTH_KEYS
    at the OAuth client JSON. Any desktop-OAuth flow produces both.
    """
    creds = json.loads(Path(os.environ["MAIL_CREDENTIALS"]).read_text())
    keys = json.loads(Path(os.environ["MAIL_OAUTH_KEYS"]).read_text())
    keys = keys.get("installed", keys)
    body = urllib.parse.urlencode({
        "client_id": keys["client_id"], "client_secret": keys["client_secret"],
        "refresh_token": creds["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    d = get(keys.get("token_uri", "https://oauth2.googleapis.com/token"), data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
    return str(d["access_token"])


def confirmations(window_days: int) -> list[str]:
    """(from, subject) blobs for every application confirmation in the window."""
    auth = {"Authorization": f"Bearer {mail_token()}"}
    q = (f"newer_than:{window_days}d (from:({' OR '.join(ATS_SENDERS)}) OR "
         f"subject:({' OR '.join(chr(34) + s + chr(34) for s in APPLIED_SUBJECTS)}))")
    base = ("https://gmail.googleapis.com/gmail/v1/users/me/messages"
            f"?maxResults=500&q={urllib.parse.quote(q)}")

    # Page to the end: a truncated listing yields a false "not applied", which is the
    # one failure this check exists to prevent.
    ids: list[str] = []
    page: str | None = None
    while True:
        listing = get(base + (f"&pageToken={page}" if page else ""), headers=auth)
        ids.extend(m["id"] for m in listing.get("messages", []))
        page = listing.get("nextPageToken")
        if not page:
            break

    out = []
    for mid in ids:
        meta = get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
                   "?format=metadata&metadataHeaders=From&metadataHeaders=Subject",
                   headers=auth)
        h = {x["name"].lower(): x["value"]
             for x in meta.get("payload", {}).get("headers", [])}
        out.append(f"{h.get('from', '')} {h.get('subject', '')}")
    return out


def squash(name: str) -> str:
    """'Acme Software, Inc.' -> 'acme', so it compares equal to 'Acme'."""
    n = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", name.lower())).strip()
    changed = True
    while changed:
        changed = False
        for suffix in CORP_SUFFIXES:
            if n.endswith(suffix):
                n, changed = n[: -len(suffix)].rstrip(), True
    return re.sub(r"\s+", "", n)


def already_applied(company: str, blobs: list[str]) -> str | None:
    key = squash(company)
    if len(key) < 3:
        return None
    for blob in blobs:
        if key in squash(blob):
            return blob[:100]
        # A short name like "N1" collides inside a squashed blob, so demand a real word.
        if len(key) <= 4 and re.search(rf"\b{re.escape(company)}\b", blob, re.I):
            return blob[:100]
    return None


# --- main ----------------------------------------------------------------------------


def load_companies() -> list[tuple[str, str, str]]:
    out = []
    for line in COMPANIES.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split(None, 2)
        if len(parts) >= 2:
            out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else parts[1]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3, help="freshness window (default 3)")
    ap.add_argument("--applied-window", type=int, default=15,
                    help="mail lookback for duplicates (default 15)")
    ap.add_argument("--no-mail", action="store_true", help="skip the duplicate check")
    args = ap.parse_args()

    companies = load_companies()
    if not companies:
        print(f"no boards listed in {COMPANIES}", file=sys.stderr)
        return 1

    def pull(entry: tuple[str, str, str]) -> list[dict[str, Any]] | None:
        ats, slug, name = entry
        fetch = FETCHERS.get(ats)
        if fetch is None:
            return []
        try:
            return fetch(slug, name)
        except (OSError, KeyError, json.JSONDecodeError, ValueError):
            return None

    rows: list[dict[str, Any]] = []
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        for got in pool.map(pull, companies):
            if got is None:
                failed += 1
            else:
                rows.extend(got)
                ok += 1
    print(f"fetched {len(rows)} live postings from {ok} boards ({failed} unreachable)")

    drops: dict[str, int] = {}

    def drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    kept = []
    for r in rows:
        r["level"] = ("newgrad" if NEWGRAD.search(r["title"])
                      else "entry" if JUNIOR.search(r["title"]) else "unlabeled")
        title, loc = r["title"], r["loc"] or ""
        if not ROLE.search(title) or SENIOR.search(title):
            drop("not the role, or senior")
        elif INTERNSHIP.search(title):
            drop("internship")
        elif squash(r["company"]) in {squash(b) for b in BLOCKED_EMPLOYERS} \
                or CLEARED_SITES.search(loc):
            drop("blocked employer or cleared site")
        elif NON_US.search(loc):
            drop("outside the US")
        elif r["published"] is None:
            drop("no first-publish date")  # cannot prove freshness, so refuse it
        elif (NOW - r["published"]).days > args.days:
            drop("older than the window")
        else:
            kept.append(r)

    if kept:  # body pass over survivors only
        with ThreadPoolExecutor(max_workers=8) as pool:
            bodies = list(pool.map(body_of, kept))
        survivors = []
        for r, body in zip(kept, bodies):
            text = re.sub(r"<[^>]+>", " ", body)
            if INELIGIBLE.search(text):
                drop("clearance or ITAR in body")
            elif EXCLUDES_VISA.search(text):
                drop("excludes this visa category")
            elif (floor := years_floor(text)) is not None and floor >= 3:
                drop("3+ years required")
            else:
                if floor == 2 and r["level"] == "unlabeled":
                    r["level"] = "reach"
                survivors.append(r)
        kept = survivors

    seen: set[tuple[str, str]] = set()
    deduped = []
    for r in kept:
        k = (norm(r["company"]), norm(r["title"]))
        if k in seen:
            drop("duplicate row")
            continue
        seen.add(k)
        deduped.append(r)
    kept = deduped

    skipped: list[tuple[str, str, str]] = []
    if kept and not args.no_mail:
        try:
            blobs = confirmations(args.applied_window)
            print(f"mail: {len(blobs)} application confirmations in "
                  f"{args.applied_window} days")
            survivors = []
            for r in kept:
                hit = already_applied(r["company"], blobs)
                if hit:
                    skipped.append((r["company"], r["title"], hit))
                    drop("already applied")
                else:
                    survivors.append(r)
            kept = survivors
        except (OSError, KeyError, FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"mail check skipped ({type(exc).__name__}: {exc})", file=sys.stderr)

    rank = {"newgrad": 0, "entry": 1, "unlabeled": 2, "reach": 3}
    kept.sort(key=lambda r: (rank[r["level"]], -r["published"].timestamp()))

    def cell(v: str) -> str:
        # A raw pipe inside a value splits the row: locations like
        # "San Francisco, CA | New York, NY" silently gain a column.
        return (v or "-").replace("|", "/").strip()

    lines = [f"# Roles first published in the last {args.days} days", "",
             f"_Generated {NOW:%Y-%m-%d %H:%M} UTC. Every row is live on the employer's own "
             f"board, was FIRST published within {args.days} days, states no bar of 3+ years, "
             "and carries no clearance, ITAR or visa-category exclusion._", "",
             "| Company | Role | Level | Location | First posted | Source | Apply |",
             "|---|---|---|---|---|---|---|"]
    for r in kept:
        age = (NOW - r["published"]).days
        lines.append(f"| {cell(r['company'])} | {cell(r['title'])} | {r['level']} "
                     f"| {cell(r['loc'])} | {'today' if age <= 0 else f'{age}d ago'} "
                     f"| {r['source']} | [apply]({r['url']}) |")
    if not kept:
        lines.append("| - | nothing new in the window | - | - | - | - | - |")

    lines += ["", "## Dropped", "", *(f"- {k}: {v}" for k, v in sorted(drops.items()) if v)]
    if skipped:
        lines += ["", "### Already applied", ""]
        lines += [f"- {c} - {t}  \n  matched: `{s}`" for c, t, s in skipped]

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(kept)} role(s)")
    print(f"  dropped: {drops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
