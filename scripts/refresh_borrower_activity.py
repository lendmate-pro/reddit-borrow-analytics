"""Refreshes data/reddit_activity_cache.json — how recently and how often each Loyalty-tab
borrower has posted or commented anywhere on Reddit, pulled from the Arctic Shift archive.

Not part of the hourly pipeline on purpose: it's one archive lookup per borrower (two requests,
comments + posts), and re-running it every hour for a list that only grows would mean hammering
a third-party archive for a signal that doesn't change meaningfully hour to hour. Run this by
hand — or on whatever schedule you like — then run hourly_update.py (or just let the next
scheduled run happen) to pick up the refreshed cache. Only fetches borrowers not already cached,
so re-runs are cheap.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
POSTS_PATH = DATA / "posts_raw.jsonl"
LOANS_FINAL_PATH = DATA / "loans_final.json"
CACHE_PATH = DATA / "reddit_activity_cache.json"

LOYALTY_WINDOW_DAYS = 182
BASE_COMMENTS = "https://arctic-shift.photon-reddit.com/api/comments/search"
BASE_POSTS = "https://arctic-shift.photon-reddit.com/api/posts/search"
UA = "Mozilla/5.0 (research; contact jyoth.antony@gmail.com)"


def loyalty_borrowers():
    loans = json.loads(LOANS_FINAL_PATH.read_text())
    by_borrower = defaultdict(list)
    for l in loans:
        if l["borrower"]:
            by_borrower[l["borrower"]].append(l)
    cutoff = time.time() - LOYALTY_WINDOW_DAYS * 86400
    out = []
    for borrower, ls in by_borrower.items():
        if len(ls) != 1:
            continue
        loan = ls[0]
        if loan["is_repaid"] and loan["created_utc"] >= cutoff:
            out.append(borrower)
    return out


def fetch(kind, base, user, retries=3):
    params = {"author": user, "limit": 100, "fields": "created_utc"}
    url = base + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8")).get("data") or []
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            wait = 2 * (attempt + 1)
            print(f"  retry {attempt + 1}/{retries} for {kind}/{user} after {e} (sleeping {wait}s)", flush=True)
            time.sleep(wait)
    return None


def analyze(user):
    comments = fetch("comments", BASE_COMMENTS, user)
    posts = fetch("posts", BASE_POSTS, user)
    if comments is None and posts is None:
        return None
    timestamps = [it["created_utc"] for it in (comments or []) + (posts or []) if it.get("created_utc")]
    if not timestamps:
        return {"last_activity_utc": None, "count_90d": 0}
    now = time.time()
    return {
        "last_activity_utc": max(timestamps),
        "count_90d": sum(1 for t in timestamps if now - t <= 90 * 86400),
    }


def main():
    borrowers = loyalty_borrowers()
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    todo = [b for b in borrowers if b not in cache]
    print(f"Loyalty borrowers: {len(borrowers)}, already cached: {len(borrowers) - len(todo)}, to fetch: {len(todo)}", flush=True)

    for i, user in enumerate(todo, 1):
        result = analyze(user)
        if result is not None:
            cache[user] = result
        if i % 20 == 0 or i == len(todo):
            CACHE_PATH.write_text(json.dumps(cache))
            print(f"  progress: {i}/{len(todo)}", flush=True)
        time.sleep(0.15)

    CACHE_PATH.write_text(json.dumps(cache))
    print(f"Done. Cache now has {len(cache)} borrowers.", flush=True)


if __name__ == "__main__":
    main()
