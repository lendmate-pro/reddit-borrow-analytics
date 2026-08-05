import json
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "unpaid_flags.jsonl"

BASE_COMMENTS = "https://arctic-shift.photon-reddit.com/api/comments/search"
UA = "Mozilla/5.0 (research; contact jyoth.antony@gmail.com)"

INFO_RE = re.compile(r"Here is my information on /u/(?P<user>\S+):")
UNPAID_ROW_RE = re.compile(
    r"^(?P<lender>[^|\n]+)\|(?P<borrower>[^|]+)\|(?P<given>[\d,]+\.\d{2}) (?P<currency>[A-Za-z]{3})\|"
    r"(?P<repaid>[\d,]+\.\d{2}) [A-Za-z]{3}\|\*\*\*UNPAID\*\*\*\|(?P<thread>[^|]*)\|(?P<date_given>[^|\n]*)\|?\s*$",
    re.MULTILINE,
)

AFTER_START = 1770312377   # full archive start
BEFORE_END = 1785967376    # full archive end (+1s)


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
            wait = min(6 * (attempt + 1), 30)
            print(f"  retry {attempt + 1}/{retries} after error: {e} (sleeping {wait}s)", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {retries} retries: {url}")


def main():
    cursor = AFTER_START
    total_fetched = 0
    total_info_comments = 0
    total_unpaid_rows = 0
    seen_row_keys = set()

    with open(OUT_PATH, "w") as out:
        while cursor < BEFORE_END:
            params = {
                "subreddit": "borrow", "author": "LoansBot", "after": cursor, "before": BEFORE_END,
                "limit": 100, "sort": "asc", "fields": "body,created_utc,id",
            }
            url = BASE_COMMENTS + "?" + urllib.parse.urlencode(params)
            page = http_get_json(url)
            if not page:
                break

            for c in page:
                body = c.get("body") or ""
                if not INFO_RE.search(body):
                    continue
                total_info_comments += 1
                for m in UNPAID_ROW_RE.finditer(body):
                    row = m.groupdict()
                    key = (row["lender"].lower(), row["borrower"].lower(), row["given"], row["date_given"].strip())
                    if key in seen_row_keys:
                        continue
                    seen_row_keys.add(key)
                    out.write(json.dumps({
                        "lender": row["lender"], "borrower": row["borrower"],
                        "amount": float(row["given"].replace(",", "")), "currency": row["currency"],
                        "thread": row["thread"].strip(), "date_given": row["date_given"].strip(),
                        "seen_in_comment_utc": c.get("created_utc"),
                    }) + "\n")
                    total_unpaid_rows += 1

            total_fetched += len(page)
            cursor = page[-1]["created_utc"] + 1
            print(f"fetched={total_fetched} info_comments={total_info_comments} unpaid_rows={total_unpaid_rows} "
                  f"cursor={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(cursor))}", flush=True)
            time.sleep(1.2)

    print(f"DONE. total_fetched={total_fetched} info_comments={total_info_comments} unique_unpaid_rows={total_unpaid_rows}", flush=True)


if __name__ == "__main__":
    main()
