"""
FA Job Bot v2
- check   : every 20 min, scans all watched companies, alerts new FA roles (0-3 yrs) on Telegram
- discover: weekly, tries every name in company_names.txt on Greenhouse/Lever/Ashby, saves hits
- digest  : Sunday, sends list of jobs blocked for "no sponsorship" so you can spot mistakes
"""
import json, os, re, html, sys, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

SEEN_FILE, SKIPPED_FILE = "seen_jobs.json", "skipped_jobs.json"
DISCOVERED_FILE, MANUAL_FILE, NAMES_FILE = "discovered.json", "companies.json", "company_names.txt"
TG_TOKEN, TG_CHAT = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
HEADERS = {"User-Agent": "Mozilla/5.0 (job-alert-bot)"}
WORKERS = 20
SKIP_NO_SPONSOR = True   # False = still alert, tagged ❌

# ================= FILTERS =================
TITLE_INCLUDE = re.compile(
    r"(financial analyst|finance analyst|fp&a|fp\s*&\s*a|financial planning|"
    r"strategic finance analyst|corporate finance analyst|business finance analyst)", re.I)
TITLE_EXCLUDE = re.compile(
    r"\b(senior|sr\.?|lead|manager|director|principal|staff|head|vp|"
    r"vice president|ii|iii|iv|intern|internship|co-?op)\b", re.I)
LOCATION_EXCLUDE = re.compile(r"(india|philippines|poland|mexico|brazil|costa rica|"
                              r"united kingdom|london|canada|toronto|germany|ireland)", re.I)

def clean(text):
    t = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(text or "")))
    return re.sub(r"\s+", " ", t)

def years_check(text):
    if not text:
        return True, "yrs not stated"
    t = clean(text).lower()
    for lo, hi in re.findall(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\+?\s*(?:years|yrs)", t):
        lo, hi = int(lo), int(hi)
        if hi <= 15:
            return (lo <= 1 and hi <= 3), f"{lo}-{hi} yrs"
    m = re.search(r"(\d{1,2})\+?\s*(?:years|yrs)", t)
    if m and int(m.group(1)) <= 15:
        n = int(m.group(1))
        return n <= 2, f"{n}+ yrs"
    if re.search(r"entry[- ]level|new grad|recent graduate", t):
        return True, "entry level"
    return True, "yrs not stated"

NO_SPONSOR = re.compile(
    r"(will not|won't|unable to|cannot|can ?not|do not|does not|not able to|are not able to)\s+"
    r"(offer |provide |support |)(visa |immigration |employment |work |)sponsor"
    r"|without (the need for |requiring |needing |)(current or future |now or in the future |future |)"
    r"(visa |immigration |employment |)sponsorship"
    r"|not (be |)eligible for (visa |immigration |)sponsorship"
    r"|sponsorship (is not|will not be|isn't|not) (available|provided|offered|supported)"
    r"|no (visa |immigration |)sponsorship"
    r"|\bno (h-?1b|opt|cpt|stem opt)\b"
    r"|(h-?1b|opt|cpt)\b[^.]{0,40}\bnot (eligible|accepted|considered|supported)"
    r"|u\.?s\.? citizens? (only|required)|must be a u\.?s\.? citizen"
    r"|green card holders? only|security clearance", re.I)
YES_SPONSOR = re.compile(
    r"(will|can|may|does|do) (offer |provide |consider |)(visa |h-?1b |)sponsor"
    r"|sponsorship (is |)(available|provided|offered|considered)"
    r"|opt (and|/) ?cpt (candidates |)(welcome|accepted|eligible)", re.I)
AUTH_ONLY = re.compile(r"authori[sz]ed to work in the (u\.?s|united states)|work authori[sz]ation", re.I)

def sponsorship_check(text):
    if not text:
        return "unclear", "⚠️ JD not loaded, check sponsorship"
    t = clean(text)
    m = NO_SPONSOR.search(t)
    if m:
        return "no", f"❌ No sponsorship: \"{m.group(0)[:60]}\""
    if YES_SPONSOR.search(t):
        return "yes", "✅ Sponsorship mentioned as available"
    if AUTH_ONLY.search(t):
        return "unclear", "⚠️ Says 'authorized to work', verify"
    return "unclear", "⚠️ Sponsorship not mentioned"

def title_ok(job):
    return (TITLE_INCLUDE.search(job["title"]) and not TITLE_EXCLUDE.search(job["title"])
            and not LOCATION_EXCLUDE.search(job.get("location") or ""))

# ================= HTTP =================
def get_json(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=25, **kw)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None

# ================= FETCHERS =================
# Each returns list of jobs. "desc_fn" loads the description only if the title matches (saves time).
def greenhouse(slug, name):
    r = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs") or {}
    return [{"id": f"gh-{slug}-{j['id']}", "company": name, "title": j["title"],
             "location": (j.get("location") or {}).get("name", ""), "url": j["absolute_url"],
             "desc_fn": (lambda jid=j["id"]: (get_json(
                 f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}") or {}).get("content", ""))}
            for j in r.get("jobs", [])]

def lever(slug, name):
    r = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json") or []
    if not isinstance(r, list):
        return []
    return [{"id": f"lv-{slug}-{j['id']}", "company": name, "title": j.get("text", ""),
             "location": (j.get("categories") or {}).get("location", ""), "url": j.get("hostedUrl", ""),
             "desc": (j.get("descriptionPlain") or "") + " " +
                     " ".join(l.get("content", "") for l in j.get("lists", []))} for j in r]

def ashby(slug, name):
    r = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}") or {}
    return [{"id": f"ab-{slug}-{j['id']}", "company": name, "title": j.get("title", ""),
             "location": j.get("location", ""), "url": j.get("jobUrl", ""),
             "desc": j.get("descriptionPlain") or j.get("descriptionHtml", "")} for j in r.get("jobs", [])]

def smartrecruiters(slug, name):
    r = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?q=financial%20analyst") or {}
    def detail(jid):
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{jid}") or {}
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return " ".join((v or {}).get("text", "") for v in secs.values())
    return [{"id": f"sr-{slug}-{j['id']}", "company": name, "title": j.get("name", ""),
             "location": (j.get("location") or {}).get("city", ""),
             "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
             "desc_fn": (lambda jid=j["id"]: detail(jid))} for j in r.get("content", [])]

def workday(cfg, name):
    tenant, wd, site = cfg.split("|")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    r = requests.post(f"{base}/wday/cxs/{tenant}/{site}/jobs",
                      json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "financial analyst"},
                      headers={**HEADERS, "Content-Type": "application/json"}, timeout=25)
    data = r.json() if r.status_code == 200 else {}
    out = []
    for j in data.get("jobPostings", []):
        path = j.get("externalPath", "")
        out.append({"id": f"wd-{tenant}-{path}", "company": name, "title": j.get("title", ""),
                    "location": j.get("locationsText", ""), "url": f"{base}/{site}{path}",
                    "posted": j.get("postedOn", ""),
                    "desc_fn": (lambda p=path: ((get_json(f"{base}/wday/cxs/{tenant}/{site}{p}") or {})
                                .get("jobPostingInfo") or {}).get("jobDescription", ""))})
    return out

FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
            "smartrecruiters": smartrecruiters, "workday": workday}

# ================= TELEGRAM =================
def send_text(msg):
    if TG_TOKEN and TG_CHAT:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=30)
    print(msg, "\n")

def notify(job, yrs, spon):
    send_text(f"🚨 <b>{html.escape(job['title'])}</b>\n🏢 {html.escape(job['company'])}\n"
              f"📍 {html.escape(job.get('location') or 'n/a')}\n🎯 {yrs}\n{html.escape(spon)}"
              f"{(' | ' + job['posted']) if job.get('posted') else ''}\n🔗 {job['url']}")

# ================= FILE HELPERS =================
def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

def all_companies():
    seen_keys, out = set(), []
    for c in load(MANUAL_FILE, []) + load(DISCOVERED_FILE, []):
        if "REPLACE_ME" in c.get("slug", ""):
            continue
        key = f"{c['ats']}:{c['slug'].lower()}"
        if key not in seen_keys:
            seen_keys.add(key); out.append(c)
    return out

# ================= MODE: CHECK =================
def check():
    state = load(SEEN_FILE, {})
    if isinstance(state, list):  # old format
        state = {"jobs": state, "companies": []}
    seen, known = set(state.get("jobs", [])), set(state.get("companies", []))
    skipped = load(SKIPPED_FILE, [])
    companies = all_companies()

    def fetch(c):
        try:
            return c, FETCHERS[c["ats"]](c["slug"], c["name"])
        except Exception as e:
            print(f"[skip] {c['name']}: {e}")
            return c, []

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(fetch, companies))

    candidates = []
    for c, jobs in results:
        key = f"{c['ats']}:{c['slug'].lower()}"
        is_new_company = key not in known   # first time seeing this company: record jobs, no alerts
        known.add(key)
        for j in jobs:
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            if not is_new_company and title_ok(j):
                candidates.append(j)

    def load_desc(j):
        if "desc_fn" in j:
            try:
                j["desc"] = j.pop("desc_fn")()
            except Exception:
                j["desc"] = ""
        return j

    with ThreadPoolExecutor(WORKERS) as ex:
        candidates = list(ex.map(load_desc, candidates))

    sent = 0
    for j in candidates:
        keep, yrs = years_check(j.get("desc", ""))
        if not keep:
            continue
        bucket, spon = sponsorship_check(j.get("desc", ""))
        if bucket == "no" and SKIP_NO_SPONSOR:
            skipped.append({"company": j["company"], "title": j["title"], "url": j["url"],
                            "reason": spon, "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
            continue
        notify(j, yrs, spon); sent += 1

    json.dump({"jobs": sorted(seen), "companies": sorted(known)}, open(SEEN_FILE, "w"))
    json.dump(skipped[-500:], open(SKIPPED_FILE, "w"), indent=1)
    print(f"Done. Watched {len(companies)} companies, {sent} alerts sent.")

# ================= MODE: DISCOVER =================
def slug_variants(name):
    base = re.sub(r"[.,'’&()!]", "", name.lower())
    base = re.sub(r"\b(inc|llc|corp|corporation|co|company|group|holdings|technologies|the)\b", "", base).strip()
    words = base.split()
    v = ["".join(words), "-".join(words)]
    return list(dict.fromkeys(x for x in v if len(x) >= 3))

def probe(name):
    for slug in slug_variants(name):
        g = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if g and g.get("name"):
            return {"name": g["name"], "ats": "greenhouse", "slug": slug, "query": name}
        l = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
        if isinstance(l, list):
            return {"name": name, "ats": "lever", "slug": slug, "query": name}
        a = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if a and isinstance(a.get("jobs"), list):
            return {"name": name, "ats": "ashby", "slug": slug, "query": name}
    return None

def discover():
    names = [n.strip() for n in open(NAMES_FILE) if n.strip() and not n.startswith("#")]
    found = load(DISCOVERED_FILE, [])
    done = {f.get("query", "").lower() for f in found}
    todo = list(dict.fromkeys(n for n in names if n.lower() not in done))
    print(f"Probing {len(todo)} new names...")
    with ThreadPoolExecutor(WORKERS) as ex:
        hits = [h for h in ex.map(probe, todo) if h]
    found += hits
    json.dump(found, open(DISCOVERED_FILE, "w"), indent=1)
    msg = (f"🔎 <b>Discovery done</b>\nChecked {len(todo)} names, found {len(hits)} on "
           f"Greenhouse/Lever/Ashby.\nNow watching {len(all_companies())} companies.")
    if hits:
        msg += "\nNew: " + ", ".join(html.escape(h["name"]) for h in hits[:40])
        if len(hits) > 40:
            msg += f" +{len(hits) - 40} more"
    send_text(msg)

# ================= MODE: DIGEST =================
def digest():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    week = [x for x in load(SKIPPED_FILE, []) if x.get("date", "") >= cutoff]
    if not week:
        send_text("📋 <b>Weekly check</b>\nNo jobs were blocked for sponsorship this week."); return
    lines = [f"📋 <b>Weekly check: {len(week)} jobs blocked for sponsorship</b>",
             "Scan for mistakes. Tap any that look wrong.\n"]
    for x in week:
        lines.append(f"• {html.escape(x['company'])}: {html.escape(x['title'])}\n"
                     f"  {html.escape(x['reason'])}\n  {x['url']}")
    msg = ""
    for line in lines:
        if len(msg) + len(line) > 3800:
            send_text(msg); msg = ""
        msg += line + "\n"
    if msg:
        send_text(msg)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": check, "discover": discover, "digest": digest}[mode]()
