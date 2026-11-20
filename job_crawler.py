"""Daily remote-job digest.

Pulls postings from remote job boards and from company career pages (watchlist),
keeps only the ones that match your criteria and were posted in the last day,
and emails you a digest. Run daily by GitHub Actions (see README_SETUP.md).

Edit the CONFIG section below to tune what you get.
"""
import html
import json
import unicodedata
import os
import re
import smtplib
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import requests

# =============================== CONFIG ===============================

MAX_AGE_HOURS = 24          # only roles posted within this window
REMOTIVE_MAX_AGE_HOURS = 48  # Remotive's free feed runs behind, so give it extra room

# A role must have one of these in its TITLE.
TITLE_KEYWORDS = [
    "producer", "game producer", "technical producer", "production manager",
    "project manager", "program manager", "technical project manager",
    "technical program manager", "product manager", "technical product manager",
    "product owner", "ecommerce", "e-commerce", "creative operations",
    "operations manager", "delivery manager", "scrum master", "release manager",
    "game operations", "live ops", "liveops", "product operations",
]

# Skip a role if any of these appear in its title or company name...
EXCLUDE_TERMS = [
    "casino", "gambling", "gambler", "sportsbook", "sports betting", "betting",
    "igaming", "wagering", "social casino", "slot machine", "lottery",
    "insurance", "insurer", "insurtech",
]
# ...or if they appear this many times in the description (benefit boilerplate
# like "health insurance" is ignored so ordinary benefits sections don't trigger it).
DESCRIPTION_HITS_TO_EXCLUDE = 2
INSURANCE_HITS_TO_EXCLUDE = 3

# Flag roles that mention flexible / unlimited time off.
PTO_PATTERNS = [
    r"unlimited (?:pto|paid time off|vacation|time off|leave|holiday)",
    r"flexible (?:pto|paid time off|time off|vacation)",
    r"flex(?:ible)? pto",
    r"open (?:pto|vacation)",
    r"discretionary (?:pto|time off|vacation)",
    r"self[- ]managed (?:pto|time off)",
    r"take what you need",
]

REMOTIVE_URL = "https://remotive.com/api/remote-jobs?limit=500"
REMOTEOK_URL = "https://remoteok.com/api"
WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
    "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
]

WATCHLIST_FILE = "watchlist.txt"
SEEN_FILE = "seen.json"
SEEN_KEEP_DAYS = 60
HEADERS = {"User-Agent": "personal-job-digest/1.0 (personal use)"}
TIMEOUT = 30

# ============================ END OF CONFIG ============================


@dataclass
class Job:
    title: str
    company: str
    url: str
    location: str
    posted: datetime | None
    text: str
    source: str
    remote: bool = True        # board is remote-only, or the posting says remote
    watch: bool = False        # came from a watchlist company
    pto: bool = False


# ------------------------------ helpers ------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_ms(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def word_regex(words):
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


TITLE_RE = word_regex(TITLE_KEYWORDS)
EXCLUDE_RE = word_regex(EXCLUDE_TERMS)
PTO_RE = re.compile("|".join(PTO_PATTERNS), re.I)
BENEFIT_INSURANCE_RE = re.compile(
    r"\b(?:health|medical|dental|vision|life|disability|supplemental|accident|pet|travel|"
    r"unemployment|national|social)\b[\w ,&/-]{0,40}?\binsurance\b|\binsurance\s+"
    r"(?:benefits?|coverage|plans?|premiums?)\b",
    re.I,
)
INSURANCE_RE = word_regex(["insurance", "insurer", "insurtech"])
GAMBLING_RE = word_regex([t for t in EXCLUDE_TERMS if "insur" not in t])


def location_ok(loc):
    """Accept US-friendly or location-agnostic remote roles."""
    l = (loc or "").lower().strip()
    if not l or re.fullmatch(r"[\s\-–,()]*(remote|anywhere|worldwide|global)[\s\-–,()]*", l):
        return True
    return bool(re.search(
        r"\b(usa|u\.s\.a?\.?|us|united states|america|americas|north america|"
        r"worldwide|anywhere|global|ohio|eastern|est|et)\b", l))


def excluded(job):
    if EXCLUDE_RE.search(f"{job.title} {job.company}"):
        return True
    body = job.text
    if len(GAMBLING_RE.findall(body)) >= DESCRIPTION_HITS_TO_EXCLUDE:
        return True
    cleaned = BENEFIT_INSURANCE_RE.sub(" ", body)
    return len(INSURANCE_RE.findall(cleaned)) >= INSURANCE_HITS_TO_EXCLUDE


# ------------------------------ sources ------------------------------

def fetch_remotive():
    out = []
    for j in get_json(REMOTIVE_URL).get("jobs", []):
        out.append(Job(
            title=j.get("title", ""), company=j.get("company_name", ""),
            url=j.get("url", ""), location=j.get("candidate_required_location", ""),
            posted=parse_iso(j.get("publication_date")),
            text=strip_html(j.get("description")), source="Remotive"))
    return out


def fetch_remoteok():
    data = get_json(REMOTEOK_URL)
    out = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is a legal notice
        out.append(Job(
            title=j.get("position", ""), company=j.get("company", ""),
            url=j.get("url", ""), location=j.get("location", ""),
            posted=parse_iso(j.get("date")), text=strip_html(j.get("description")),
            source="RemoteOK"))
    return out


def fetch_wwr():
    out, errors = [], []
    for feed in WWR_FEEDS:
        try:
            r = requests.get(feed, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:  # keep going if one feed fails
            errors.append(f"{feed.rsplit('/', 1)[-1]}: {e}")
            continue
        for it in root.iter("item"):
            raw_title = (it.findtext("title") or "").strip()
            company, _, title = raw_title.partition(": ")
            if not title:
                company, title = "", raw_title
            try:
                posted = parsedate_to_datetime(it.findtext("pubDate"))
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                posted = None
            out.append(Job(
                title=title, company=company, url=(it.findtext("link") or "").strip(),
                location=it.findtext("region") or "", posted=posted,
                text=strip_html(it.findtext("description")), source="We Work Remotely"))
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    return out


BOARD_SOURCES = [("Remotive", fetch_remotive), ("RemoteOK", fetch_remoteok),
                 ("We Work Remotely", fetch_wwr)]


# ---- company career pages (applicant tracking systems with public feeds) ----

def greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        out.append(Job(
            title=j.get("title", ""), company="", url=j.get("absolute_url", ""),
            location=loc, posted=parse_iso(j.get("first_published") or j.get("updated_at")),
            text=strip_html(j.get("content")), source="Greenhouse",
            remote="remote" in f"{loc} {j.get('title', '')}".lower()))
    return out


def lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        raise ValueError("unexpected Lever response")
    out = []
    for j in data:
        loc = (j.get("categories") or {}).get("location", "") or ""
        out.append(Job(
            title=j.get("text", ""), company="", url=j.get("hostedUrl", ""),
            location=loc, posted=parse_ms(j.get("createdAt")),
            text=j.get("descriptionPlain") or strip_html(j.get("description")),
            source="Lever",
            remote=(j.get("workplaceType") == "remote") or "remote" in loc.lower()))
    return out


def ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data["jobs"]:
        loc = j.get("location", "") or ""
        out.append(Job(
            title=j.get("title", ""), company="", url=j.get("jobUrl", ""),
            location=loc, posted=parse_iso(j.get("publishedAt")),
            text=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            source="Ashby",
            remote=bool(j.get("isRemote")) or "remote" in loc.lower()))
    return out


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def slug_guesses(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"\.com\b", "", name.lower())
    base = re.sub(r"\b(inc|llc|the company|international)\b\.?", "", base).strip()
    joined = re.sub(r"[^a-z0-9]", "", base)
    dashed = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return list(dict.fromkeys([joined, dashed, joined + "com"]))


def load_watchlist():
    """Lines look like:  Company Name   or   Company Name | greenhouse:slug"""
    entries = []
    if not os.path.exists(WATCHLIST_FILE):
        return entries
    for line in open(WATCHLIST_FILE, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, pin = (p.strip() for p in line.partition("|"))
        entries.append((name, pin))
    return entries


def fetch_watchlist():
    jobs, report = [], []
    for name, pin in load_watchlist():
        attempts = []
        if pin and ":" in pin:
            ats, slug = pin.split(":", 1)
            attempts = [(ats.strip().lower(), slug.strip())]
        else:
            attempts = [(ats, s) for s in slug_guesses(name) for ats in ATS]
        found = False
        for ats, slug in attempts:
            fn = ATS.get(ats)
            if not fn:
                continue
            try:
                got = fn(slug)
            except Exception:
                continue
            for j in got:
                j.company, j.watch = name, True
            jobs += got
            sample = got[0].url if got else "no openings listed"
            report.append(f"{name}: found on {ats} ({slug}), {len(got)} openings. Sample: {sample}")
            found = True
            break
        if not found:
            report.append(f"{name}: no public Greenhouse/Lever/Ashby board found "
                          f"(check its careers page or set up a site alert)")
    return jobs, report


# ------------------------------ pipeline ------------------------------

def load_seen():
    try:
        return json.load(open(SEEN_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def job_keys(j):
    url = j.url.split("?")[0].rstrip("/").lower()
    return [url, f"{j.company.lower().strip()}|{j.title.lower().strip()}"]


def select(jobs, seen):
    cutoff = now_utc()
    keep, dup_keys = [], set()
    for j in jobs:
        if not j.url or not TITLE_RE.search(j.title):
            continue
        window = REMOTIVE_MAX_AGE_HOURS if j.source == "Remotive" else MAX_AGE_HOURS
        if j.posted and j.posted < cutoff - timedelta(hours=window):
            continue
        if j.posted and j.posted > cutoff + timedelta(hours=2):
            continue
        if not j.remote or not location_ok(j.location) or excluded(j):
            continue
        keys = job_keys(j)
        if any(k in seen or k in dup_keys for k in keys):
            continue
        dup_keys.update(keys)
        j.pto = bool(PTO_RE.search(j.text))
        keep.append(j)
    keep.sort(key=lambda j: (not j.watch, not j.pto,
                             -(j.posted or datetime(1970, 1, 1, tzinfo=timezone.utc)).timestamp()))
    return keep


def render(matches, health, watch_report):
    def when(j):
        if not j.posted:
            return "date not listed"
        hrs = max(0, int((now_utc() - j.posted).total_seconds() // 3600))
        return f"{hrs}h ago"

    rows, text = [], []
    for j in matches:
        tags = ("⭐ " if j.watch else "") + ("🌴 PTO " if j.pto else "")
        company = j.company or "Company not listed"
        rows.append(
            f'<p><b>{tags}<a href="{html.escape(j.url)}">{html.escape(j.title)}</a></b><br>'
            f'{html.escape(company)} · {html.escape(j.location or "Remote")} · '
            f'{j.source} · {when(j)}</p>')
        text.append(f"{tags}{j.title} | {company} | {j.location or 'Remote'} | {j.source} | "
                    f"{when(j)}\n{j.url}\n")
    if not matches:
        rows.append("<p>No new matches in the last day.</p>")
        text.append("No new matches in the last day.\n")

    notes = [f"{n}: {c} fetched" + (f" (ERROR: {e})" if e else "") for n, c, e in health]
    notes += watch_report
    rows.append("<hr><p style='color:#666;font-size:12px'><b>Source health</b><br>"
                + "<br>".join(html.escape(n) for n in notes)
                + "<br>Boards: Remotive, RemoteOK, We Work Remotely.</p>")
    text.append("\nSource health:\n" + "\n".join(notes))
    return "".join(rows), "\n".join(text)


def send_email(subject, html_body, text_body):
    sender = os.environ["GMAIL_ADDRESS"]
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, sender
    msg["To"] = os.environ.get("TO_ADDRESS", sender)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    seen = load_seen()
    jobs, health = [], []
    for name, fn in BOARD_SOURCES:
        try:
            got = fn()
            jobs += got
            health.append((name, len(got), None))
        except Exception as e:
            health.append((name, 0, str(e)[:150]))
    wjobs, watch_report = fetch_watchlist()
    jobs += wjobs

    matches = select(jobs, seen)
    html_body, text_body = render(matches, health, watch_report)
    subject = f"Job digest: {len(matches)} new remote match{'es' if len(matches) != 1 else ''} " \
              f"({now_utc():%b %d})"

    if dry:
        print(subject, "\n", text_body)
        return
    send_email(subject, html_body, text_body)

    today = f"{now_utc():%Y-%m-%d}"
    for j in matches:
        for k in job_keys(j):
            seen[k] = today
    keep_after = f"{now_utc() - timedelta(days=SEEN_KEEP_DAYS):%Y-%m-%d}"
    seen = {k: d for k, d in seen.items() if d >= keep_after}
    json.dump(seen, open(SEEN_FILE, "w"), indent=0)
    print(subject)


if __name__ == "__main__":
    main()
