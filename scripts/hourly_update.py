import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from dateutil import parser as dateparser
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "python-dateutil"], check=True)
    from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"
TEMPLATES = ROOT / "templates"
STATE_PATH = ROOT / "state.json"
RAW_PATH = DATA / "loansbot_raw.jsonl"
POSTS_PATH = DATA / "posts_raw.jsonl"
LOANS_FINAL_PATH = DATA / "loans_final.json"
DASHBOARD_DATA_PATH = DATA / "dashboard_data_v2.json"
DOCS_DIR = ROOT / "docs"
DASHBOARD_HTML_PATH = DOCS_DIR / "index.html"
AUTH_HASH_PATH = CONFIG / "auth_hash.txt"

BASE_COMMENTS = "https://arctic-shift.photon-reddit.com/api/comments/search"
BASE_POSTS = "https://arctic-shift.photon-reddit.com/api/posts/ids"
UA = "Mozilla/5.0 (research; contact jyoth.antony@gmail.com)"

CREATE_RE = re.compile(
    r"Noted! I will remember that /u/(?P<lender>\S+) lent (?P<amount>[\d,]+\.\d{2}) USD to /u/(?P<borrower>\S+)"
)
REPAID_RE = re.compile(
    r"/u/(?P<borrower>\S+) has now repaid /u/(?P<lender>\S+) (?P<repaid_amount>[\d,]+\.\d{2}) USD",
)
TABLE_ROW_RE = re.compile(
    r"^(?P<lender>[^\|\n]+)\|(?P<borrower>[^\|]+)\|(?P<given>[\d,]+\.\d{2}) USD\|(?P<repaid>[\d,]+\.\d{2}) USD\|(?P<unpaid>[^\|]*)\|(?P<thread>[^\|]*)\|(?P<date_given>[^\|]*)\|(?P<date_paid>[^\|]*)\|(?P<loan_id>\d+)\s*$",
    re.MULTILINE,
)
LOANID_RE = re.compile(r"id=(\d+)")


def http_get_json(url, retries=6):
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data.get("data") or []
        except Exception as e:
            wait = min(5 * (attempt + 1), 30)
            print(f"  retry {attempt + 1}/{retries} after error: {e} (sleeping {wait}s)", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {retries} retries: {url}")


def fetch_new_comments(after, before):
    """Fetch and parse all LoansBot loan-creation/repayment comments in (after, before]."""
    cursor = after
    new_records = []
    while cursor < before:
        params = {
            "subreddit": "borrow", "author": "LoansBot", "after": cursor, "before": before,
            "limit": 100, "sort": "asc", "fields": "body,created_utc,link_id,id",
        }
        url = BASE_COMMENTS + "?" + urllib.parse.urlencode(params)
        page = http_get_json(url)
        if not page:
            break
        for c in page:
            body = c.get("body") or ""
            created = c.get("created_utc")
            link_id = c.get("link_id")
            cid = c.get("id")

            m = CREATE_RE.search(body)
            if m:
                new_records.append({
                    "type": "create", "lender": m.group("lender"), "borrower": m.group("borrower"),
                    "amount": float(m.group("amount").replace(",", "")),
                    "created_utc": created, "link_id": link_id, "comment_id": cid,
                })
                continue
            m = REPAID_RE.search(body)
            if m:
                rows = TABLE_ROW_RE.findall(body)
                last_row = rows[-1] if rows else None
                rec = {
                    "type": "repaid", "lender": m.group("lender"), "borrower": m.group("borrower"),
                    "repaid_amount": float(m.group("repaid_amount").replace(",", "")),
                    "created_utc": created, "link_id": link_id, "comment_id": cid,
                }
                if last_row:
                    rec["loan_id"] = last_row[8]
                    rec["amount_given"] = float(last_row[2].replace(",", ""))
                    rec["amount_repaid_total"] = float(last_row[3].replace(",", ""))
                    rec["date_given"] = last_row[6].strip()
                    rec["date_paid_back"] = last_row[7].strip()
                    rec["thread"] = last_row[5].strip()
                new_records.append(rec)
        cursor = page[-1]["created_utc"] + 1
        print(f"  fetched page: {len(page)} comments, {len(new_records)} matched so far, cursor={cursor}", flush=True)
        time.sleep(1.2)
    return new_records


def fetch_posts_batch(ids):
    out = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        params = {"ids": ",".join(chunk), "fields": "id,title,selftext,created_utc,author"}
        url = BASE_POSTS + "?" + urllib.parse.urlencode(params)
        out.extend(http_get_json(url))
        time.sleep(1.2)
    return out


# ---------- Aggregation logic (mirrors the dashboard's original build pipeline) ----------

def find_repay_amount(title, principal):
    t = title.replace("&", "$")
    m = re.search(r"repay(?:ment)?\s*(?:of\s*)?\$?\s*(\d[\d,]*(?:\.\d+)?)", t, re.IGNORECASE)
    if m:
        val = float(m.group(1).replace(",", ""))
        if val > principal * 1.001:
            return val
    all_amounts = [float(x.replace(",", "")) for x in re.findall(r"\$\s*(\d[\d,]*(?:\.\d+)?)", t)]
    all_amounts += [float(x.replace(",", "")) for x in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*\$", t)]
    bigger = [a for a in all_amounts if a > principal * 1.001]
    if bigger:
        return min(bigger)
    return None


def find_title_principal(title):
    t = title.replace("&", "$")
    m = re.search(r"\(\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:USD)?\s*\)", t)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)", t)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


DATE_PATTERNS = [
    r"\d{1,2}/\d{1,2}/\d{2,4}",
    r"\d{1,2}-\d{1,2}-\d{2,4}",
    r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}",
    r"\d{1,2}/\d{1,2}(?!\d)",
]
DATE_RE = re.compile("|".join(f"(?:{p})" for p in DATE_PATTERNS))


def find_scheduled_date(title, post_created_utc):
    candidates = DATE_RE.findall(title)
    post_dt = datetime.fromtimestamp(post_created_utc, tz=timezone.utc)
    best = None
    for c in candidates:
        try:
            dt = dateparser.parse(c, default=datetime(post_dt.year, post_dt.month, post_dt.day, tzinfo=timezone.utc))
        except (ValueError, OverflowError):
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < post_dt:
            try:
                dt2 = dt.replace(year=dt.year + 1)
                if dt2 >= post_dt:
                    dt = dt2
            except ValueError:
                pass
        delta_days = (dt - post_dt).days
        if 0 <= delta_days <= 400:
            if best is None or dt < best:
                best = dt
    return best


def parse_date_str(s):
    if not s or not s.strip():
        return None
    try:
        dt = dateparser.parse(s.strip())
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OverflowError):
        return None


def rebuild_loans_final():
    posts = {}
    for line in open(POSTS_PATH):
        p = json.loads(line)
        posts[p["id"]] = p

    creates = []
    repaids_by_loan = defaultdict(list)
    for line in open(RAW_PATH):
        r = json.loads(line)
        if r["type"] == "create":
            raw_borrower = r["borrower"]
            m = LOANID_RE.search(raw_borrower)
            loan_id = m.group(1) if m else None
            clean_borrower = raw_borrower.split("[")[0]
            creates.append({
                "loan_id": loan_id, "lender": r["lender"], "borrower": clean_borrower,
                "principal": r["amount"], "link_id": r["link_id"].replace("t3_", ""),
                "created_utc": r["created_utc"],
            })
        else:
            if r.get("loan_id"):
                repaids_by_loan[r["loan_id"]].append(r)

    repaid_final = {}
    for lid, recs in repaids_by_loan.items():
        recs.sort(key=lambda x: x["created_utc"])
        repaid_final[lid] = recs[-1]

    loans = []
    seen_loan_ids = set()
    for c in creates:
        lid = c["loan_id"]
        if lid:
            if lid in seen_loan_ids:
                continue
            seen_loan_ids.add(lid)

        post = posts.get(c["link_id"])
        title = post["title"] if post else ""
        post_created = post["created_utc"] if post else c["created_utc"]

        repaid_rec = repaid_final.get(lid) if lid else None
        is_repaid = bool(repaid_rec and repaid_rec.get("date_paid_back"))

        date_given_dt = parse_date_str(repaid_rec["date_given"]) if repaid_rec else None
        date_paid_dt = parse_date_str(repaid_rec["date_paid_back"]) if repaid_rec else None
        scheduled_dt = find_scheduled_date(title, post_created) if title else None

        if is_repaid and date_given_dt and date_paid_dt:
            duration_days = (date_paid_dt - date_given_dt).days
        elif scheduled_dt:
            duration_days = (scheduled_dt - datetime.fromtimestamp(post_created, tz=timezone.utc)).days
        else:
            duration_days = None

        repay_amount = find_repay_amount(title, c["principal"]) if title else None
        title_principal = find_title_principal(title) if title else None
        fee_pct = None
        is_split_funded = title_principal is not None and (
            c["principal"] < title_principal * 0.85 or c["principal"] > title_principal * 1.15
        )
        if repay_amount and not is_split_funded:
            candidate_fee = (repay_amount - c["principal"]) / c["principal"] * 100
            if 0 < candidate_fee <= 150:
                fee_pct = candidate_fee

        loans.append({
            "loan_id": lid, "lender": c["lender"], "borrower": c["borrower"], "principal": c["principal"],
            "fee_pct": fee_pct,
            "duration_days": duration_days if duration_days is not None and 0 <= duration_days <= 400 else None,
            "is_repaid": is_repaid, "created_utc": c["created_utc"], "post_id": c["link_id"],
        })

    json.dump(loans, open(LOANS_FINAL_PATH, "w"))
    return loans


def build_dashboard_data(loans):
    compact = [{"l": l["lender"], "b": l["borrower"], "p": l["principal"], "f": l["fee_pct"],
                "d": l["duration_days"], "r": 1 if l["is_repaid"] else 0, "t": l["created_utc"],
                "u": l["post_id"]} for l in loans]
    out = {
        "loans": compact,
        "range": {"min_t": min(c["t"] for c in compact), "max_t": max(c["t"] for c in compact)},
    }
    data_str = json.dumps(out, separators=(",", ":"))
    DASHBOARD_DATA_PATH.write_text(data_str)
    return data_str


def build_dashboard_html(data_str, last_updated_label):
    tpl = (TEMPLATES / "dashboard_template.html").read_text()
    auth_hash = AUTH_HASH_PATH.read_text().strip()
    out = (tpl.replace("__DASHBOARD_DATA__", data_str)
              .replace("__AUTH_HASH__", auth_hash)
              .replace("__LAST_UPDATED__", last_updated_label))
    DOCS_DIR.mkdir(exist_ok=True)
    DASHBOARD_HTML_PATH.write_text(out)


def main():
    state = json.loads(STATE_PATH.read_text())
    last_cursor = state["last_cursor"]
    now = int(time.time())

    print(f"Fetching new comments from {last_cursor} to {now}...", flush=True)
    new_records = fetch_new_comments(last_cursor, now)
    print(f"New matched loan events: {len(new_records)}", flush=True)

    if new_records:
        existing_comment_ids = set()
        for line in open(RAW_PATH):
            existing_comment_ids.add(json.loads(line).get("comment_id"))
        new_records = [r for r in new_records if r.get("comment_id") not in existing_comment_ids]

    if new_records:
        with open(RAW_PATH, "a") as f:
            for r in new_records:
                f.write(json.dumps(r) + "\n")

        existing_post_ids = set()
        for line in open(POSTS_PATH):
            existing_post_ids.add(json.loads(line)["id"])
        needed_ids = {r["link_id"].replace("t3_", "") for r in new_records} - existing_post_ids
        if needed_ids:
            print(f"Fetching {len(needed_ids)} new post titles...", flush=True)
            new_posts = fetch_posts_batch(sorted(needed_ids))
            with open(POSTS_PATH, "a") as f:
                for p in new_posts:
                    f.write(json.dumps(p) + "\n")

        max_new_created = max(r["created_utc"] for r in new_records)
        state["last_cursor"] = max_new_created + 1
    else:
        state["last_cursor"] = now

    state["last_run_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    STATE_PATH.write_text(json.dumps(state, indent=2))

    print("Rebuilding loans_final.json from full accumulated archive...", flush=True)
    loans = rebuild_loans_final()
    print(f"Total loans in archive: {len(loans)}", flush=True)

    print("Rebuilding dashboard_data_v2.json...", flush=True)
    data_str = build_dashboard_data(loans)

    print("Rebuilding dashboard.html...", flush=True)
    IST = timezone(timedelta(hours=5, minutes=30))
    last_updated_label = "Data last refreshed: " + datetime.now(IST).strftime("%b %d, %Y %H:%M IST")
    build_dashboard_html(data_str, last_updated_label)

    print(f"DONE. new_events={len(new_records)} total_loans={len(loans)} last_cursor={state['last_cursor']}", flush=True)


if __name__ == "__main__":
    main()
