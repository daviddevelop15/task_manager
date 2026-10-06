"""Job-application analytics for the Task Manager daily board.

Reads every daily-board card (each card = one applied job) plus the Meetings
sheet (jobs that reached the interview stage) and writes a detailed report to
``doc/analyst.md``: trending tech stacks, popular titles/positions, job sites,
volume over time, and application -> interview conversion.

The database is encrypted (password mode). This script works on a COPY of the
encrypted files in a temp folder and never modifies your live data. Run it and
type your app password when prompted:

    py -3 analyze_jobs.py

The password may also be supplied via the TM_DB_PASSWORD environment variable
(useful for non-interactive runs).
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime
from html import unescape
from pathlib import Path

# --- where the live data lives ----------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
INTERNAL_DIR = PROJECT_DIR / "dist" / "TaskManager" / "_internal"
OUTPUT_PATH = PROJECT_DIR / "doc" / "analyst.md"

_PRESERVE = (
    "task_manager.db",
    "task_manager.db.enc",
    "task_manager.db.enc.bak",
    "task_manager.vault",
    "task_manager.vault.bak",
    "task_manager_acc.key",
)


# --- text / field parsing ---------------------------------------------------

_TAG_RE = re.compile(r"(?s)<[^>]+>")


def plain_text(body: str) -> str:
    """Plain text from a card body (handles HTML bodies)."""
    b = body or ""
    if "<" in b and ">" in b:
        b = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", b)
        b = re.sub(r"(?is)<br\s*/?>", "\n", b)
        b = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", b)
        b = _TAG_RE.sub(" ", b)
        b = unescape(b)
    return b


def labeled(text: str, label: str) -> str:
    pat = re.compile(rf"^\s*{re.escape(label)}\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
    m = pat.search(text or "")
    return m.group(1).strip() if m else ""


def role_from_title(title: str) -> str:
    """Card title minus a leading 'Site - ' source prefix."""
    t = (title or "").strip()
    m = re.match(r"^.+?\s[-–—]\s+(.+)$", t)
    return (m.group(1) if m else t).strip()


def site_from_title(title: str) -> str:
    m = re.match(r"^(.+?)\s[-–—]\s+.+$", (title or "").strip())
    return m.group(1).strip() if m else ""


_COMPOUND_TLDS = frozenset(
    {"co.uk", "com.au", "co.jp", "co.in", "co.nz", "com.br", "co.za", "com.mx",
     "co.kr", "com.sg", "com.hk", "co.il", "com.tr", "com.cn", "co.id",
     "com.ph", "com.my", "co.th", "com.ar", "com.co"}
)


def site_from_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    import urllib.parse

    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) < 2:
        return host
    if len(labels) >= 3 and ".".join(labels[-2:]) in _COMPOUND_TLDS:
        core = labels[:-2]
    else:
        core = labels[:-1]
    return core[-1] if core else host


def norm_company(name: str) -> str:
    noise = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co",
             "company", "gmbh", "srl", "sa", "plc", "pvt", "llp", "the",
             "group", "holdings", "technologies", "technology", "labs",
             "software", "solutions"}
    s = re.sub(r"[^\w\s]", " ", (name or "").lower())
    return " ".join(t for t in s.split() if t and t not in noise)


def norm_role(role: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (role or "").lower()).split())


# --- taxonomies -------------------------------------------------------------

# category -> [(canonical label, regex), ...]. Presence counted once per card.
TECH: dict[str, list[tuple[str, str]]] = {
    "Languages": [
        ("Python", r"\bpython\b"), ("JavaScript", r"\bjavascript\b|\bjs\b"),
        ("TypeScript", r"\btypescript\b|\bts\b"), ("Java", r"\bjava\b(?!script)"),
        ("C#", r"c#|\bc sharp\b|\bcsharp\b"), ("C++", r"c\+\+|\bcpp\b"),
        ("C", r"\bc\b(?!\+|#|ss)"), ("Go", r"\bgo\b|\bgolang\b"),
        ("Rust", r"\brust\b"), ("Ruby", r"\bruby\b"), ("PHP", r"\bphp\b"),
        ("Kotlin", r"\bkotlin\b"), ("Swift", r"\bswift\b"), ("Scala", r"\bscala\b"),
        ("R", r"\br\b(?= programming| language)"), ("Perl", r"\bperl\b"),
        ("Elixir", r"\belixir\b"), ("Dart", r"\bdart\b"),
        ("SQL", r"\bsql\b"), ("Bash/Shell", r"\bbash\b|\bshell script"),
    ],
    "Frontend": [
        ("React", r"\breact(?:\.js|js)?\b"), ("Angular", r"\bangular\b"),
        ("Vue", r"\bvue(?:\.js|js)?\b"), ("Next.js", r"\bnext\.?js\b"),
        ("Svelte", r"\bsvelte\b"), ("Redux", r"\bredux\b"),
        ("HTML", r"\bhtml5?\b"), ("CSS", r"\bcss3?\b"), ("SASS/SCSS", r"\bsass\b|\bscss\b"),
        ("Tailwind", r"\btailwind\b"), ("jQuery", r"\bjquery\b"),
        ("Webpack", r"\bwebpack\b"), ("Vite", r"\bvite\b"),
    ],
    "Backend": [
        ("Node.js", r"\bnode(?:\.js|js)?\b"), ("Express", r"\bexpress(?:\.js)?\b"),
        ("Django", r"\bdjango\b"), ("Flask", r"\bflask\b"), ("FastAPI", r"\bfastapi\b"),
        ("Spring", r"\bspring\b"), (".NET", r"\.net\b|\bdotnet\b|asp\.net"),
        ("Rails", r"\brails\b|ruby on rails"), ("Laravel", r"\blaravel\b"),
        ("GraphQL", r"\bgraphql\b"), ("REST", r"\brest(?:ful)?\b|\brest api"),
        ("gRPC", r"\bgrpc\b"), ("Nest.js", r"\bnest\.?js\b"),
        ("Microservices", r"\bmicroservices?\b"),
    ],
    "Databases": [
        ("PostgreSQL", r"\bpostgres(?:ql)?\b"), ("MySQL", r"\bmysql\b"),
        ("MongoDB", r"\bmongo(?:db)?\b"), ("Redis", r"\bredis\b"),
        ("SQLite", r"\bsqlite\b"), ("SQL Server", r"\bsql server\b|\bmssql\b"),
        ("Oracle", r"\boracle\b"), ("DynamoDB", r"\bdynamo(?:db)?\b"),
        ("Cassandra", r"\bcassandra\b"), ("Elasticsearch", r"\belasticsearch\b|\belastic search\b"),
        ("Snowflake", r"\bsnowflake\b"), ("BigQuery", r"\bbigquery\b"),
    ],
    "Cloud & DevOps": [
        ("AWS", r"\baws\b|amazon web services"), ("Azure", r"\bazure\b"),
        ("GCP", r"\bgcp\b|google cloud"), ("Docker", r"\bdocker\b"),
        ("Kubernetes", r"\bkubernetes\b|\bk8s\b"), ("Terraform", r"\bterraform\b"),
        ("CI/CD", r"\bci/cd\b|\bci-cd\b|continuous integration"),
        ("Jenkins", r"\bjenkins\b"), ("GitHub Actions", r"github actions"),
        ("Ansible", r"\bansible\b"), ("Kafka", r"\bkafka\b"),
        ("RabbitMQ", r"\brabbitmq\b"), ("Serverless", r"\bserverless\b|\blambda\b"),
        ("Linux", r"\blinux\b"), ("Nginx", r"\bnginx\b"),
    ],
    "AI / ML / Data": [
        ("Machine Learning", r"\bmachine learning\b|\bml\b"),
        ("Deep Learning", r"\bdeep learning\b"), ("NLP", r"\bnlp\b|natural language"),
        ("LLM", r"\bllm(?:s)?\b|large language model"), ("RAG", r"\brag\b|retrieval augmented"),
        ("PyTorch", r"\bpytorch\b"), ("TensorFlow", r"\btensorflow\b"),
        ("Pandas", r"\bpandas\b"), ("NumPy", r"\bnumpy\b"),
        ("scikit-learn", r"scikit[- ]?learn|\bsklearn\b"),
        ("LangChain", r"\blangchain\b"), ("OpenAI", r"\bopenai\b|\bgpt\b"),
        ("Computer Vision", r"computer vision|\bopencv\b"),
        ("Spark", r"\bspark\b|pyspark"), ("Airflow", r"\bairflow\b"),
    ],
    "Mobile": [
        ("iOS", r"\bios\b"), ("Android", r"\bandroid\b"),
        ("React Native", r"react native"), ("Flutter", r"\bflutter\b"),
        ("SwiftUI", r"\bswiftui\b"),
    ],
    "Practices": [
        ("Agile/Scrum", r"\bagile\b|\bscrum\b"), ("TDD", r"\btdd\b|test.driven"),
        ("Git", r"\bgit\b(?!hub actions)"), ("Jira", r"\bjira\b"),
        ("Unit Testing", r"unit test"), ("System Design", r"system design"),
    ],
}

# Flatten to (label, category, compiled) preserving first-category ownership.
_TECH_FLAT: list[tuple[str, str, re.Pattern]] = []
_seen_labels: set[str] = set()
for _cat, _items in TECH.items():
    for _label, _rx in _items:
        if _label in _seen_labels:
            continue
        _seen_labels.add(_label)
        _TECH_FLAT.append((_label, _cat, re.compile(_rx, re.IGNORECASE)))

TECH_CATEGORY = {label: cat for label, cat, _ in _TECH_FLAT}


def detect_tech(text: str) -> set[str]:
    t = text or ""
    return {label for label, _cat, rx in _TECH_FLAT if rx.search(t)}


# Role family — first match wins, so order from most to least specific.
ROLE_FAMILIES: list[tuple[str, str]] = [
    ("Full-Stack", r"full[\s-]?stack"),
    ("Data Science", r"data scientist|machine learning|ml engineer|ai engineer|\bnlp\b|deep learning"),
    ("Data Engineering", r"data engineer|data analyst|analytics engineer|\bbi\b|business intelligence"),
    ("DevOps / SRE / Cloud", r"devops|\bsre\b|site reliability|platform engineer|cloud engineer|infrastructure"),
    ("Mobile", r"\bmobile\b|\bios\b|\bandroid\b|react native|flutter"),
    ("QA / Test", r"\bqa\b|quality assurance|test engineer|\bsdet\b|automation engineer"),
    ("Security", r"security|infosec|appsec|penetration"),
    ("Frontend", r"front[\s-]?end|frontend|\bui engineer\b|web developer|react developer"),
    ("Backend", r"back[\s-]?end|backend|\bapi\b|server[\s-]?side"),
    ("Product / Project", r"product manager|program manager|project manager|product owner|scrum master"),
    ("Design / UX", r"designer|\bux\b|\bui/ux\b|user experience"),
    ("Management", r"engineering manager|\bhead of\b|director|\bvp\b|team lead"),
]
_ROLE_FAMILY_RX = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in ROLE_FAMILIES]


def role_family(role: str) -> str:
    for name, rx in _ROLE_FAMILY_RX:
        if rx.search(role or ""):
            return name
    if re.search(r"engineer|developer|programmer", role or "", re.IGNORECASE):
        return "Software Engineer (general)"
    return "Other"


SENIORITY = [
    ("Intern", r"\bintern\b|\binternship\b"),
    ("Junior / Entry", r"\bjunior\b|\bjr\b|\bentry[\s-]?level\b|\bgraduate\b|\bassociate\b"),
    ("Lead / Staff / Principal", r"\blead\b|\bstaff\b|\bprincipal\b|\barchitect\b"),
    ("Manager / Director", r"\bmanager\b|\bdirector\b|\bhead of\b|\bvp\b"),
    ("Senior", r"\bsenior\b|\bsr\b"),
]
_SENIORITY_RX = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in SENIORITY]


def seniority(role: str) -> str:
    for name, rx in _SENIORITY_RX:
        if rx.search(role or ""):
            return name
    return "Mid / Unspecified"


# --- extraction -------------------------------------------------------------


def extract_records(conn: sqlite3.Connection):
    """(applications, interviews) as lists of dicts parsed from the DB."""
    conn.row_factory = sqlite3.Row

    lists = {r["id"]: r for r in conn.execute(
        "SELECT id, day, title FROM daily_lists"
    ).fetchall()}

    apps = []
    for c in conn.execute(
        "SELECT id, list_id, title, body, status, created_at FROM daily_cards"
    ).fetchall():
        title = c["title"] or ""
        body_txt = plain_text(c["body"] or "")
        if not (title.strip() or body_txt.strip()):
            continue  # skip empty placeholder cards
        company = labeled(body_txt, "Company")
        url = labeled(body_txt, "URL") or labeled(body_txt, "Company Link")
        role = role_from_title(title)
        site = site_from_url(url) or site_from_title(title)
        col = lists.get(c["list_id"], {})
        scan = f"{title}\n{body_txt}"
        apps.append({
            "id": c["id"],
            "title": title,
            "company": company,
            "role": role,
            "site": site or "(unknown)",
            "url": url,
            "status": (c["status"] or "").strip(),
            "created_at": c["created_at"] or "",
            "day": (col["day"] if col else "") or (c["created_at"] or "")[:10],
            "column": col["title"] if col else "",
            "tech": detect_tech(scan),
            "family": role_family(role),
            "seniority": seniority(role),
            "n_company": norm_company(company),
            "n_role": norm_role(role),
        })

    interviews = []
    try:
        rows = conn.execute(
            "SELECT id, title, body, link, start_at, status, created_at FROM meetings"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for m in rows:
        title = m["title"] or ""
        body_txt = plain_text(m["body"] or "")
        company = labeled(body_txt, "Company")
        role = role_from_title(title)
        interviews.append({
            "id": m["id"],
            "title": title,
            "company": company,
            "role": role,
            "status": (m["status"] or "").strip(),
            "start_at": m["start_at"] or "",
            "n_company": norm_company(company) or norm_company(title),
            "n_role": norm_role(role),
            "tech": detect_tech(f"{title}\n{body_txt}"),
        })
    return apps, interviews


# --- report helpers ---------------------------------------------------------


def bar(n: int, top: int, width: int = 28) -> str:
    if top <= 0:
        return ""
    return "█" * max(1, round(width * n / top)) if n else ""


def pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.1f}%" if total else "0%"


def ranked_table(counter: Counter, total: int, header: str, limit: int = 20) -> list[str]:
    if not counter:
        return [f"_No {header.lower()} found._", ""]
    top = counter.most_common(1)[0][1]
    out = [f"| {header} | Count | Share | |", "|---|---:|---:|:--|"]
    for name, n in counter.most_common(limit):
        out.append(f"| {name} | {n} | {pct(n, total)} | {bar(n, top)} |")
    out.append("")
    return out


def analyze(apps: list[dict], interviews: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(apps)
    L: list[str] = []
    w = L.append

    w("# Job Application Analysis")
    w("")
    w(f"_Generated {now} from the Task Manager daily board "
      f"({total} applications, {len(interviews)} interview records)._")
    w("")

    if total == 0:
        w("No applications found in the daily board.")
        return "\n".join(L)

    # dates
    days = sorted(a["day"] for a in apps if a["day"])
    first_day, last_day = (days[0], days[-1]) if days else ("?", "?")
    distinct_days = len(set(days))
    companies = Counter(a["company"] for a in apps if a["company"])
    sites = Counter(a["site"] for a in apps)
    per_day = Counter(a["day"] for a in apps if a["day"])
    busiest = per_day.most_common(1)[0] if per_day else ("?", 0)
    span_days = 1
    try:
        span_days = max(1, (date.fromisoformat(last_day) - date.fromisoformat(first_day)).days + 1)
    except Exception:
        pass

    # --- overview ---
    w("## 1. Overview")
    w("")
    w("| Metric | Value |")
    w("|---|---|")
    w(f"| Total applications | **{total}** |")
    w(f"| Date range | {first_day} → {last_day} ({span_days} days) |")
    w(f"| Active days (with ≥1 application) | {distinct_days} |")
    w(f"| Avg applications / active day | {total / max(1, distinct_days):.1f} |")
    w(f"| Avg applications / calendar day | {total / span_days:.2f} |")
    w(f"| Busiest day | {busiest[0]} ({busiest[1]} applications) |")
    w(f"| Distinct companies | {len(companies)} |")
    w(f"| Distinct job sites | {len(sites)} |")
    w(f"| Interviews reached (Meetings) | {len(interviews)} |")
    w("")

    # --- job sites ---
    w("## 2. Job sites")
    w("")
    w("Where applications were sourced from (site of the job link, else the card's title prefix).")
    w("")
    L.extend(ranked_table(sites, total, "Site"))

    # --- titles / positions ---
    w("## 3. Job titles & positions")
    w("")
    w("### 3.1 Role families")
    w("")
    fam = Counter(a["family"] for a in apps)
    L.extend(ranked_table(fam, total, "Role family"))
    w("### 3.2 Seniority")
    w("")
    sen = Counter(a["seniority"] for a in apps)
    L.extend(ranked_table(sen, total, "Seniority"))
    w("### 3.3 Most common exact titles")
    w("")
    titles = Counter(a["role"].strip() for a in apps if a["role"].strip())
    L.extend(ranked_table(titles, total, "Job title", limit=25))

    # --- tech stacks ---
    w("## 4. Tech stacks")
    w("")
    tech_counter = Counter()
    for a in apps:
        tech_counter.update(a["tech"])
    apps_with_tech = sum(1 for a in apps if a["tech"])
    w(f"Detected technologies in **{apps_with_tech}** of {total} applications "
      f"({pct(apps_with_tech, total)}). Each technology counts once per application.")
    w("")
    w("### 4.1 Most in-demand technologies (overall)")
    w("")
    L.extend(ranked_table(tech_counter, total, "Technology", limit=30))

    w("### 4.2 By category")
    w("")
    for cat in TECH:
        cat_counter = Counter({k: v for k, v in tech_counter.items()
                               if TECH_CATEGORY.get(k) == cat})
        if not cat_counter:
            continue
        w(f"**{cat}**")
        w("")
        L.extend(ranked_table(cat_counter, total, "Technology", limit=15))

    # co-occurrence
    w("### 4.3 Technologies that appear together")
    w("")
    pair_counter = Counter()
    for a in apps:
        ts = sorted(a["tech"])
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                pair_counter[(ts[i], ts[j])] += 1
    if pair_counter:
        top = pair_counter.most_common(1)[0][1]
        w("| Pairing | Count | |")
        w("|---|---:|:--|")
        for (a1, a2), n in pair_counter.most_common(15):
            w(f"| {a1} + {a2} | {n} | {bar(n, top)} |")
        w("")
    else:
        w("_Not enough tech signal for pairings._")
        w("")

    # --- companies ---
    w("## 5. Companies")
    w("")
    w(f"You applied to **{len(companies)}** distinct companies.")
    w("")
    repeats = Counter({c: n for c, n in companies.items() if n > 1})
    if repeats:
        w("### Companies applied to more than once")
        w("")
        L.extend(ranked_table(repeats, total, "Company", limit=20))
    else:
        w("_No company was applied to more than once._")
        w("")

    # --- status funnel ---
    w("## 6. Application status")
    w("")
    statuses = Counter(a["status"] or "(no status)" for a in apps)
    L.extend(ranked_table(statuses, total, "Status"))

    # --- over time ---
    w("## 7. Applications over time")
    w("")
    by_month = Counter(a["day"][:7] for a in apps if len(a["day"]) >= 7)
    if by_month:
        top = by_month.most_common(1)[0][1]
        w("### 7.1 By month")
        w("")
        w("| Month | Count | |")
        w("|---|---:|:--|")
        for month in sorted(by_month):
            n = by_month[month]
            w(f"| {month} | {n} | {bar(n, top)} |")
        w("")
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow = Counter()
    for a in apps:
        try:
            dow[date.fromisoformat(a["day"]).weekday()] += 1
        except Exception:
            pass
    if dow:
        top = max(dow.values())
        w("### 7.2 By weekday")
        w("")
        w("| Weekday | Count | |")
        w("|---|---:|:--|")
        for i in range(7):
            n = dow.get(i, 0)
            w(f"| {dow_names[i]} | {n} | {bar(n, top)} |")
        w("")

    # --- interview conversion ---
    w("## 8. Interview stage (Meetings)")
    w("")
    if not interviews:
        w("_No interview records found in the Meetings sheet._")
        w("")
    else:
        interviewed_companies = {i["n_company"] for i in interviews if i["n_company"]}
        conv_apps = [a for a in apps if a["n_company"] and a["n_company"] in interviewed_companies]
        conv_rate = pct(len(conv_apps), total)
        w(f"- Interview records: **{len(interviews)}**")
        w(f"- Distinct companies reached interview: **{len(interviewed_companies)}**")
        w(f"- Applications whose company reached an interview: **{len(conv_apps)}** ({conv_rate} of all applications)")
        w("")
        w("> Matching is heuristic: an application is counted as 'reached interview' "
          "when its company name matches a Meetings entry. Titles vary between the "
          "application and the calendar invite, so company is the reliable key.")
        w("")
        # which sites convert
        site_applied = Counter(a["site"] for a in apps)
        site_conv = Counter(a["site"] for a in conv_apps)
        rows = [(s, site_conv.get(s, 0), site_applied[s]) for s in site_applied]
        rows = [r for r in rows if r[2] >= 1]
        rows.sort(key=lambda r: (r[1] / r[2] if r[2] else 0, r[1]), reverse=True)
        if any(r[1] for r in rows):
            w("### 8.1 Interview rate by site")
            w("")
            w("| Site | Interviews | Applications | Rate |")
            w("|---|---:|---:|---:|")
            for s, ci, ap in rows[:15]:
                if ci:
                    w(f"| {s} | {ci} | {ap} | {pct(ci, ap)} |")
            w("")
        # which tech converts
        tech_applied = Counter()
        tech_conv = Counter()
        for a in apps:
            tech_applied.update(a["tech"])
        for a in conv_apps:
            tech_conv.update(a["tech"])
        conv_rows = [(t, tech_conv.get(t, 0), tech_applied[t]) for t in tech_applied
                     if tech_applied[t] >= 2 and tech_conv.get(t, 0) > 0]
        conv_rows.sort(key=lambda r: (r[1] / r[2], r[1]), reverse=True)
        if conv_rows:
            w("### 8.2 Interview rate by technology (>=2 applications)")
            w("")
            w("| Technology | Interviews | Applications | Rate |")
            w("|---|---:|---:|---:|")
            for t, ci, ap in conv_rows[:20]:
                w(f"| {t} | {ci} | {ap} | {pct(ci, ap)} |")
            w("")

    # --- insights ---
    w("## 9. Highlights")
    w("")
    if sites:
        s, n = sites.most_common(1)[0]
        w(f"- **Top source:** {s} — {n} applications ({pct(n, total)}).")
    if fam:
        f, n = fam.most_common(1)[0]
        w(f"- **Most-targeted role family:** {f} ({pct(n, total)} of applications).")
    if tech_counter:
        top3 = ", ".join(f"{k} ({v})" for k, v in tech_counter.most_common(3))
        w(f"- **Most-demanded tech:** {top3}.")
    if by_month and len(by_month) >= 2:
        months = sorted(by_month)
        w(f"- **Trend:** {by_month[months[0]]} applications in {months[0]} "
          f"vs {by_month[months[-1]]} in {months[-1]}.")
    if interviews:
        w(f"- **Interview conversion:** {len(conv_apps)}/{total} applications "
          f"reached the interview stage ({conv_rate}).")
    w("")
    w("---")
    w(f"_Report covers {total} applications from {first_day} to {last_day}._")
    return "\n".join(L)


# --- main -------------------------------------------------------------------


def _load_and_extract(password: str):
    """Decrypt a COPY of the live DB and extract records (live data untouched)."""
    if not INTERNAL_DIR.exists():
        raise SystemExit(f"Data folder not found: {INTERNAL_DIR}")

    workdir = Path(tempfile.mkdtemp(prefix="tm_analyze_"))
    try:
        for name in _PRESERVE:
            src = INTERNAL_DIR / name
            if src.exists():
                shutil.copy2(src, workdir / name)

        sys.path.insert(0, str(PROJECT_DIR))
        import secure_store
        import database as db

        db._base_dir = lambda: workdir  # type: ignore[attr-defined]
        dek = secure_store.unlock(workdir / "task_manager.vault", password)
        db.unlock(dek)
        conn = db._connect()  # decrypted in-memory copy
        apps, interviews = extract_records(conn)
        try:
            db.close()
        except Exception:
            pass
        return apps, interviews
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _password_from_env_file() -> str:
    """Read TM_DB_PASSWORD from doc/.env.var (KEY=VALUE lines), if present."""
    path = PROJECT_DIR / "doc" / ".env.var"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "TM_DB_PASSWORD":
            return val.strip().strip('"').strip("'")
    return ""


def main() -> int:
    password = os.environ.get("TM_DB_PASSWORD") or _password_from_env_file()
    if not password:
        try:
            password = getpass.getpass("Task Manager DB password: ")
        except Exception:
            print("No password found in doc/.env.var or TM_DB_PASSWORD, and no "
                  "interactive prompt available.", file=sys.stderr)
            return 2
    if not password:
        print("Empty password.", file=sys.stderr)
        return 2

    try:
        apps, interviews = _load_and_extract(password)
    except Exception as exc:  # wrong password surfaces here
        print(f"Could not open the database: {exc}", file=sys.stderr)
        print("(A wrong password is the most common cause.)", file=sys.stderr)
        return 1

    report = analyze(apps, interviews)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report, encoding="utf-8")
    print(f"Analyzed {len(apps)} applications, {len(interviews)} interviews.")
    print(f"Report written to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
