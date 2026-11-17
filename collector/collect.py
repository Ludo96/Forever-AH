#!/usr/bin/env python3
"""Hourly collector for the Forever-AH dashboard.

Pulls the current auction listings for the tracked WoW Forever auction houses
from the Battle.net API, reduces them to a few numbers per item, and keeps a
small static data set that the dashboard reads:

    data/meta.json                      what is tracked and when it last updated
    data/items.json                     item id -> name, quality, type, vendor price
    data/<house>/summary.json           one row per item listed right now
    data/<house>/h/<YYYY-MM-DD>/<shard>.json   hourly points, kept 30 days
    data/<house>/d/<YYYY-MM>/<shard>.json      daily points, kept forever
    data/<house>/p/<shard>.json         each item's price and sales by hour of day
    data/<house>/open.json.gz           the listings of the last snapshot, used
                                        to estimate sales in the next one
    data/state.json                     collector bookkeeping

The Forever API is undocumented, so every response is read defensively.
Prices are in copper per unit.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SHARDS = 64                 # history files are split by item id % SHARDS
HOURLY_DAYS = 30            # how long hourly points are kept
MIN_GAP = 20 * 60           # ignore a second snapshot within 20 minutes
MAX_NAME_LOOKUPS = 1500     # item-name lookups per run
NAME_RETRY_DAYS = 3         # how long to wait before retrying an unknown item
QUALITY = {"POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3, "EPIC": 4,
           "LEGENDARY": 5, "ARTIFACT": 6}
SUMMARY_COLS = ["id", "low", "market", "qty", "near", "listings", "low24hAgo",
                "low1dMed", "low7dMin", "low7dMed", "low7dMax", "market7dMed",
                "points7d", "low30dMin", "low30dMed", "low30dMax", "points30d",
                "sold1d", "sold7d",
                "buyHour", "buyPct", "sellHour", "sellPct", "peakHour", "hourDays"]
HOUR_MIN_DAYS = 3           # days an hour needs before it can be an item's best hour
HOURLY_N = 5                # numbers per hourly point: tIndex, low, market, qty, sold
DAILY_N = 7                 # per daily point: day, low, high, market, qty, hours, sold
LEFT = {"LONG": 1, "VERY_LONG": 2}     # time_left values that rule out expiry
SALES_METHOD = 2            # raise to discard sales figures after changing the estimate


class NotFound(Exception):
    pass


class ApiError(Exception):
    pass


# ----------------------------------------------------------------- API ----

class Api:
    """Minimal Battle.net client (client-credentials flow, read only)."""

    def __init__(self, region: str, client_id: str, client_secret: str,
                 timeout: int = 120):
        self.region = region
        self.host = f"https://{region}.api.blizzard.com"
        self.timeout = timeout
        self.token = self._fetch_token(client_id, client_secret)

    def _fetch_token(self, client_id: str, client_secret: str) -> str:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            "https://oauth.battle.net/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {basic}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)["access_token"]
        except urllib.error.HTTPError as err:
            raise ApiError(
                f"Battle.net rejected the client ID or secret (HTTP {err.code}). "
                "Check the BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET "
                "repository secrets.") from None
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as err:
            raise ApiError(f"Could not get a Battle.net token: {err}") from None

    def get(self, path: str, namespace: str, tries: int = 4):
        """GET a document. Returns (json, headers); raises NotFound on 404."""
        query = urllib.parse.urlencode({"namespace": namespace, "locale": "en_GB"})
        return self._get_url(f"{self.host}{path}?{query}", tries)

    def search(self, path: str, params: dict, namespace: str):
        """GET a search endpoint. Returns (json, headers)."""
        query = urllib.parse.urlencode({**params, "namespace": namespace, "locale": "en_GB"})
        return self._get_url(f"{self.host}{path}?{query}", 4)

    def get_href(self, href: str, tries: int = 4):
        """GET a link the API handed back (already carries its namespace)."""
        if not href.startswith(self.host):
            raise ApiError(f"unexpected link host: {href}")
        return self._get_url(href, tries)

    def _get_url(self, url: str, tries: int):
        label = urllib.parse.urlsplit(url).path
        for attempt in range(tries):
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept-Encoding": "gzip"})
            last = attempt == tries - 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw), resp.headers
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    raise NotFound(label) from None
                if err.code in (429, 500, 502, 503, 504) and not last:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise ApiError(f"{label}: HTTP {err.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    ValueError, OSError) as err:
                if not last:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise ApiError(f"{label}: {err}") from None
        raise ApiError(f"{label}: gave up")


# ------------------------------------------------------------- helpers ----

def text(value) -> str:
    """Names come back as a string, or as a {locale: string} map."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("en_GB", "en_US"):
            if isinstance(value.get(key), str):
                return value[key]
        for val in value.values():
            if isinstance(val, str):
                return val
    return ""


def ref_id(value):
    """Pull a numeric id out of a number, an {id} object or a {key:{href}} link."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("id"), int):
            return value["id"]
        href = value.get("href") or (value.get("key") or {}).get("href") or ""
        found = re.search(r"/ruleset/(\d+)", href)
        if found:
            return int(found.group(1))
    return None


def faction_of(name: str) -> str:
    low = name.lower()
    for faction in ("horde", "alliance", "blackwater"):
        if faction in low:
            return faction
    return "other"


def utc_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def http_date(value) -> int | None:
    if not value:
        return None
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError):
        return None


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def median(values) -> int:
    return round(statistics.median(values))


def log(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------ fetching ----

def list_houses(api, namespace: str, ruleset: int) -> list[dict]:
    doc = None
    # The index has flipped between needing and rejecting the trailing slash.
    for path in (f"/data/wow/ruleset/{ruleset}/auctions/",
                 f"/data/wow/ruleset/{ruleset}/auctions"):
        try:
            doc, _ = api.get(path, namespace)
            break
        except NotFound:
            continue
    if doc is None:
        raise ApiError(f"No auction house index for ruleset {ruleset}. "
                       "The undocumented Forever API may have changed.")
    raw = doc.get("auctions") or doc.get("auction_houses") or []
    houses = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("id"), int):
            houses.append({"id": entry["id"],
                           "name": text(entry.get("name")) or f"House {entry['id']}"})
    return houses


def fetch_house(api, namespace: str, ruleset: int, house_id: int) -> dict:
    base = f"/data/wow/ruleset/{ruleset}/auctions/{house_id}"
    doc, headers = api.get(base, namespace)
    auctions = doc.get("auctions")
    if isinstance(auctions, dict) and auctions.get("href"):
        # In case the listings ever move behind a link, like commodities.
        linked, headers = api.get_href(auctions["href"])
        auctions = linked.get("auctions")
    if not isinstance(auctions, list):
        auctions = []            # empty houses have no "auctions" field at all
    try:
        cdoc, _ = api.get(f"{base}/commodities", namespace)
        commodities = cdoc.get("auctions")
        if not isinstance(commodities, list):
            commodities = []
    except NotFound:
        commodities = []
    return {"auctions": auctions, "commodities": commodities,
            "ruleset": ref_id(doc.get("ruleset")),
            "last_modified": http_date(headers.get("Last-Modified"))}


# ---------------------------------------------------------- aggregation ----

def aggregate(auctions: list, commodities: list) -> tuple[dict, int]:
    """Reduce raw listings to {item id: [low, market, qty, near, listings]}.

    low      cheapest buyout per unit
    market   average unit price of the cheapest 15% of units on sale, which
             shrugs off a single lowball listing
    qty      units on sale with a buyout
    near     units priced within 10% of the low
    listings number of auctions

    Bid-only auctions carry no buyout and are skipped.
    """
    by_item: dict[int, list] = defaultdict(list)
    skipped = 0
    for entry in list(auctions) + list(commodities):
        if not isinstance(entry, dict):
            skipped += 1
            continue
        item = entry.get("item")
        item_id = item.get("id") if isinstance(item, dict) else item
        qty = entry.get("quantity") or 1
        if "unit_price" in entry:
            unit = entry.get("unit_price")
        else:
            buyout = entry.get("buyout")
            unit = buyout // qty if isinstance(buyout, int) and qty > 0 else None
        if (not isinstance(item_id, int) or isinstance(item_id, bool)
                or not isinstance(unit, int) or unit <= 0 or qty <= 0):
            skipped += 1
            continue
        by_item[item_id].append((unit, qty))

    stats = {}
    for item_id, rows in by_item.items():
        rows.sort()
        total = sum(q for _, q in rows)
        low = rows[0][0]
        take = max(1, math.ceil(total * 0.15))
        left, acc = take, 0
        for unit, q in rows:
            use = min(q, left)
            acc += unit * use
            left -= use
            if not left:
                break
        near = sum(q for unit, q in rows if unit <= low * 1.1)
        stats[item_id] = [low, round(acc / take), total, near, len(rows)]
    return stats, skipped


# -------------------------------------------------------- sales estimate ----

def open_book(auctions: list, commodities: list) -> dict:
    """Every buyable listing by auction id: {"a"|"c": {id: [item, qty, left, unit]}}.

    `left` is 2 for more than 8 hours remaining, 1 for more than 2, else 0.
    `unit` is the buyout per unit in copper.
    """
    book = {"a": {}, "c": {}}
    for kind, entries in (("a", auctions), ("c", commodities)):
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
                continue
            item = entry.get("item")
            item_id = item.get("id") if isinstance(item, dict) else item
            qty = entry.get("quantity") or 1
            unit = entry.get("unit_price")
            if unit is None and isinstance(entry.get("buyout"), int):
                unit = entry["buyout"] // max(1, qty)
            if not isinstance(item_id, int) or not isinstance(unit, int) or unit <= 0:
                continue                # bid-only listings cannot be bought out
            book[kind][str(entry["id"])] = [item_id, qty, LEFT.get(entry.get("time_left"), 0), unit]
    return book


def estimate_sold(prev: dict, cur: dict, gap: int) -> dict:
    """Units per item that were probably bought between two snapshots.

    Blizzard publishes listings, not sales, so this is an estimate built from
    what changed:

    * A listing whose quantity dropped was partly bought. Always counted.
    * A listing that vanished is counted only if it still had more time left
      than the gap between the snapshots (so it cannot have expired) AND it
      was not priced above a cheaper listing of the same item that is still
      there. Buyers take the cheapest first, so a dearer listing vanishing
      while a cheaper one stays was almost certainly cancelled.
    * For one-of-a-kind auctions (gear, recipes), newly posted listings of the
      same item are subtracted, because cancel-and-repost is the usual reason
      those vanish.

    Cancelling the cheapest listing still looks like a sale, so treat the
    result as an upper estimate.
    """
    need = 1 if gap <= 2 * 3600 else 2 if gap <= 8 * 3600 else 3
    sold: dict[int, int] = defaultdict(int)
    for kind in ("a", "c"):
        before, now = prev.get(kind, {}), cur.get(kind, {})
        cheapest_left: dict[int, int] = {}
        for auction_id, entry in before.items():
            if auction_id in now and len(entry) > 3:
                item_id, unit = entry[0], entry[3]
                if unit < cheapest_left.get(item_id, unit + 1):
                    cheapest_left[item_id] = unit
        gone: dict[int, int] = defaultdict(int)
        for auction_id, entry in before.items():
            item_id, qty, left = entry[0], entry[1], entry[2]
            unit = entry[3] if len(entry) > 3 else None
            still = now.get(auction_id)
            if still is not None:
                if still[1] < qty:
                    sold[item_id] += qty - still[1]
                continue
            if left < need:
                continue
            floor = cheapest_left.get(item_id)
            if unit is None or floor is None or unit <= floor * 1.05:
                gone[item_id] += qty
        if kind == "a":
            for auction_id, entry in now.items():
                if auction_id not in before and entry[0] in gone:
                    gone[entry[0]] -= entry[1]
        for item_id, units in gone.items():
            if units > 0:
                sold[item_id] += units
    return {k: v for k, v in sold.items() if v > 0}


def reset_sales(house_dir: Path) -> None:
    """Zero the sales figures recorded so far (used when the method changes)."""
    for path in (house_dir / "h").glob("*/*.json"):
        doc = read_json(path)
        if not doc or doc.get("n", 4) < HOURLY_N:
            continue
        for flat in doc["i"].values():
            flat[4::HOURLY_N] = [0] * len(flat[4::HOURLY_N])
        write_json(path, doc)


def read_book(house_dir: Path) -> dict | None:
    try:
        with gzip.open(house_dir / "open.json.gz", "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, EOFError):
        return None


def write_book(house_dir: Path, ts: int, book: dict) -> None:
    house_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(house_dir / "open.json.gz", "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump({"ts": ts, **book}, fh, separators=(",", ":"))


# -------------------------------------------------------------- storage ----

def widen(doc: dict, old: int, new: int) -> dict:
    """Upgrade a history file written before a field was added (pads with 0)."""
    if doc.get("n", old) < new:
        have = doc.get("n", old)
        for key, flat in doc["i"].items():
            out = []
            for k in range(0, len(flat) - have + 1, have):
                out.extend(flat[k:k + have] + [0] * (new - have))
            doc["i"][key] = out
        doc["n"] = new
    return doc


def append_hourly(house_dir: Path, ts: int, stats: dict, sold: dict | None = None) -> None:
    """Add one snapshot to today's hourly files.

    File shape: {"n": 5, "t": [timestamps],
                 "i": {id: [tIndex, low, market, qty, sold, ...]}}
    An item that sold out since the last snapshot gets a point with no price.
    """
    sold = sold or {}
    day_dir = house_dir / "h" / utc_day(ts)
    by_shard: dict[int, dict] = defaultdict(dict)
    for item_id, row in stats.items():
        by_shard[item_id % SHARDS][item_id] = [row[0], row[1], row[2], sold.get(item_id, 0)]
    for item_id, units in sold.items():
        if item_id not in stats and units > 0:
            by_shard[item_id % SHARDS][item_id] = [None, None, 0, units]
    for shard, rows in by_shard.items():
        path = day_dir / f"{shard:02d}.json"
        doc = widen(read_json(path) or {"n": HOURLY_N, "t": [], "i": {}}, 4, HOURLY_N)
        idx = len(doc["t"])
        doc["t"].append(ts)
        for item_id, row in rows.items():
            doc["i"].setdefault(str(item_id), []).extend([idx, *row])
        write_json(path, doc)


def load_shard_history(house_dir: Path, shard: int, days: list[str],
                       stamps: set | None = None) -> dict:
    """{item id: [(ts, low, market, qty, sold), ...]} for one shard, oldest first.

    low and market are None on points where the item had sold out.
    """
    hist: dict[int, list] = defaultdict(list)
    for day in days:
        doc = read_json(house_dir / "h" / day / f"{shard:02d}.json")
        if not doc:
            continue
        widen(doc, 4, HOURLY_N)
        times = doc["t"]
        if stamps is not None:
            stamps.update(times)
        for key, flat in doc["i"].items():
            points = hist[int(key)]
            for k in range(0, len(flat) - HOURLY_N + 1, HOURLY_N):
                points.append((times[flat[k]], *flat[k + 1:k + HOURLY_N]))
    return hist


def hourly_days(house_dir: Path) -> list[str]:
    root = house_dir / "h"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))


def roll_day(house_dir: Path, day: str) -> None:
    """Fold one finished UTC day into the monthly daily files.

    File shape: {"n": 7, "i": {id: [dayOfMonth, low, high, market, qty, hours, sold, ...]}}
    """
    month, dom = day[:7], int(day[8:])
    for shard in range(SHARDS):
        doc = read_json(house_dir / "h" / day / f"{shard:02d}.json")
        if not doc:
            continue
        widen(doc, 4, HOURLY_N)
        path = house_dir / "d" / month / f"{shard:02d}.json"
        monthly = widen(read_json(path) or {"n": DAILY_N, "i": {}}, 6, DAILY_N)
        for key, flat in doc["i"].items():
            lows = [v for v in flat[1::HOURLY_N] if v is not None]
            markets = [v for v in flat[2::HOURLY_N] if v is not None]
            qtys, sold = flat[3::HOURLY_N], flat[4::HOURLY_N]
            if not lows:
                continue
            old = monthly["i"].get(key, [])
            kept = []
            for k in range(0, len(old) - DAILY_N + 1, DAILY_N):
                if old[k] != dom:
                    kept.extend(old[k:k + DAILY_N])
            kept.extend([dom, min(lows), max(lows), median(markets),
                         round(sum(qtys) / len(qtys)), len(lows), sum(sold)])
            monthly["i"][key] = kept
        write_json(path, monthly)


def roll_and_prune(house_dir: Path, house_state: dict, now: int) -> None:
    today = utc_day(now)
    cutoff = utc_day(now - HOURLY_DAYS * 86400)
    rolled = set(house_state.get("rolled", []))
    for day in hourly_days(house_dir):
        if day < today and day not in rolled:
            roll_day(house_dir, day)
            rolled.add(day)
        if day < cutoff:
            shutil.rmtree(house_dir / "h" / day, ignore_errors=True)
    house_state["rolled"] = sorted(d for d in rolled if d >= cutoff)


def hour_profile(points: list, snaps: list[int], since: int):
    """How an item's price and sales vary across the 24 hours of the (UTC) day.

    price[h]  how the low in hour h compares with the item's average across
              the day (1.0 = average, 0.95 = 5% cheaper). Each snapshot is
              first divided by that day's median low so a rising or falling
              week does not leak in; days with fewer than 12 priced snapshots
              are left out
    count[h]  how many days contributed to price[h]
    sold[h]   average units sold during hour h, over the snapshots since sales
              counting began. A snapshot reports the hour before it, so sales
              are filed under the hour of (snapshot time - 30 minutes).
    """
    by_day: dict[int, list] = defaultdict(list)
    for p in points:
        if p[1] is not None:
            by_day[p[0] // 86400].append(p)
    total, count = [0.0] * 24, [0] * 24
    for rows in by_day.values():
        if len(rows) < 12:
            continue
        mid = statistics.median(r[1] for r in rows)
        for r in rows:
            hour = r[0] % 86400 // 3600
            total[hour] += r[1] / mid
            count[hour] += 1
    price = [total[h] / count[h] if count[h] else None for h in range(24)]
    known = [v for v in price if v is not None]
    if known:
        centre = sum(known) / len(known)
        price = [None if v is None else v / centre for v in price]
    units = [0] * 24
    for p in points:
        if p[4] and p[0] >= since:
            units[(p[0] - 1800) % 86400 // 3600] += p[4]
    sold = [units[h] / snaps[h] if snaps[h] else 0 for h in range(24)]
    return price, count, sold


def best_hours(price: list, count: list, sold: list, snaps: list[int]) -> list:
    """[buyHour, buyPct, sellHour, sellPct, peakHour, days] or blanks.

    Only hours seen on at least HOUR_MIN_DAYS days count, and at least 18 of
    the 24 must qualify, so one odd day cannot name the best hour.
    """
    ok = [h for h in range(24) if count[h] >= HOUR_MIN_DAYS]
    out = [None, None, None, None, None, max(count)]
    if len(ok) >= 18:
        # Weigh each hour with its neighbours (1:2:1). Picking the extreme of
        # 24 noisy averages otherwise overstates how much the hour matters.
        def smooth(h):
            near = [(price[h], 2)] + [(price[n % 24], 1) for n in (h - 1, h + 1) if count[n % 24] >= HOUR_MIN_DAYS]
            return sum(v * w for v, w in near) / sum(w for _, w in near)
        level = {h: smooth(h) for h in ok}
        buy = min(ok, key=level.get)
        sell = max(ok, key=level.get)
        out[0:4] = [buy, round(max(0, 1 - level[buy]) * 100, 1), sell, round(max(0, level[sell] - 1) * 100, 1)]
    busy = [h for h in range(24) if snaps[h] >= HOUR_MIN_DAYS]
    if len(busy) >= 18 and any(sold[h] for h in busy):
        out[4] = max(busy, key=lambda h: sold[h])
    return out


def build_summary(house_dir: Path, ts: int, stats: dict, since: int | None = None):
    """One row per item listed right now, with its recent range (SUMMARY_COLS).

    Also writes p/<shard>.json, each item's hour-of-day profile
    ([24 prices x1000, 24 units sold x10, days]), and returns the market-wide
    profile: gold traded and typical price level for each hour of the day.
    """
    days = [d for d in hourly_days(house_dir) if d >= utc_day(ts - HOURLY_DAYS * 86400)]
    since = since or ts + 1                      # no sales counted yet
    stamps: set = set()
    load_shard_history(house_dir, 0, days, stamps)
    snaps = [0] * 24                             # snapshots per sales hour
    for stamp in stamps:
        if stamp >= since:
            snaps[(stamp - 1800) % 86400 // 3600] += 1
    traded = [0.0] * 24
    levels: list[list] = [[] for _ in range(24)]
    rows = []
    for shard in range(SHARDS):
        hist = load_shard_history(house_dir, shard, days)
        profiles = {}
        for item_id, every in hist.items():
            price, count, sold = hour_profile(every, snaps, since)
            markets = [p[2] for p in every if p[2] is not None]
            worth = statistics.median(markets) if markets else 0
            for h in range(24):
                traded[h] += sold[h] * worth
                if price[h] is not None:
                    levels[h].append(price[h])
            if max(count) or any(sold):
                profiles[str(item_id)] = (
                    [None if v is None else round(v * 1000) for v in price]
                    + [round(v * 10) for v in sold] + [max(count)])
            if item_id not in stats:
                continue
            sold1d = sum(p[4] for p in every if p[0] > ts - 86400)
            sold7d = sum(p[4] for p in every if p[0] > ts - 7 * 86400)
            points = [p for p in every if p[1] is not None] or [(ts, *stats[item_id][:3], 0)]
            w30 = [p for p in points if p[0] > ts - 30 * 86400] or points[-1:]
            w7 = [p for p in w30 if p[0] > ts - 7 * 86400] or w30[-1:]
            w1 = [p for p in w7 if p[0] > ts - 86400] or w7[-1:]
            day_ago = min(w7, key=lambda p: abs(p[0] - (ts - 86400)))
            prev = day_ago[1] if abs(day_ago[0] - (ts - 86400)) <= 3 * 3600 else None
            lows7 = [p[1] for p in w7]
            lows30 = [p[1] for p in w30]
            rows.append([item_id, *stats[item_id], prev,
                         median([p[1] for p in w1]),
                         min(lows7), median(lows7), max(lows7),
                         median([p[2] for p in w7]), len(w7),
                         min(lows30), median(lows30), max(lows30), len(w30),
                         sold1d, sold7d, *best_hours(price, count, sold, snaps)])
        write_json(house_dir / "p" / f"{shard:02d}.json", {"i": profiles})
    seen = {r[0] for r in rows}
    for item_id in stats:                        # brand-new items with no history file yet
        if item_id not in seen:
            low = stats[item_id][0]
            rows.append([item_id, *stats[item_id], None, low, low, low, low,
                         stats[item_id][1], 1, low, low, low, 1, 0, 0,
                         None, None, None, None, None, 0])
    rows.sort(key=lambda r: r[0])
    level = [statistics.median(v) if len(v) >= 20 else None for v in levels]
    known = [v for v in level if v is not None]
    centre = sum(known) / len(known) if known else 1
    hours = {"traded": [round(v) for v in traded],
             "price": [None if v is None else round(v / centre, 4) for v in level],
             "snapshots": snaps, "utc": True}
    return rows, hours


# ----------------------------------------------------------- item names ----

def resolve_names(api, region: str, items: dict, misses: dict,
                  wanted: list[int], now: int, limit: int) -> None:
    """Look up names for new item ids.

    Forever's own item data is missing a large share of auctionable items, so
    each id falls back to the Classic Era and Classic namespaces before it is
    parked and retried a few days later.
    """
    todo = [i for i in wanted
            if str(i) not in items and misses.get(str(i), 0) <= now][:limit]
    if not todo:
        return
    namespaces = [f"static-forever-{region}", f"static-classic1x-{region}",
                  f"static-classic-{region}"]

    def lookup(item_id: int):
        for source, namespace in enumerate(namespaces):
            try:
                doc, _ = api.get(f"/data/wow/item/{item_id}", namespace, tries=2)
            except NotFound:
                continue
            except ApiError:
                return item_id, None, True          # transient: retry next run
            name = text(doc.get("name"))
            if not name:
                continue
            quality = QUALITY.get((doc.get("quality") or {}).get("type"), 1)
            sell = doc.get("sell_price")
            level = doc.get("level")
            return item_id, [
                name, quality,
                text((doc.get("item_class") or {}).get("name")),
                text((doc.get("item_subclass") or {}).get("name")),
                level if isinstance(level, int) else 0,
                sell if isinstance(sell, int) else 0,
                source], False
        return item_id, None, False

    found = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for item_id, record, transient in pool.map(lookup, todo):
            if record:
                items[str(item_id)] = record
                misses.pop(str(item_id), None)
                found += 1
            elif not transient:
                misses[str(item_id)] = now + NAME_RETRY_DAYS * 86400
    log(f"names: looked up {len(todo)}, found {found}")


# ----------------------------------------------------------------- run ----

def choose_houses(cfg: dict, houses: list[dict]) -> tuple[list[dict], bool]:
    pinned = cfg.get("houses")
    if isinstance(pinned, list) and pinned:
        known = {h["id"]: h for h in houses}
        return [known.get(i, {"id": i, "name": f"House {i}"}) for i in pinned], False
    factions = [f.lower() for f in cfg.get("factions", ["horde", "blackwater"])]
    return [h for h in houses if faction_of(h["name"]) in factions], True


def run(api, out: Path, cfg: dict, now: int | None = None) -> dict:
    now = int(now if now is not None else time.time())
    region = cfg.get("region", "eu")
    ruleset = int(cfg.get("ruleset", 1))
    namespace = f"dynamic-forever-{region}"
    cuts = cfg.get("auction_cut", {})
    data = Path(out) / "data"

    state = read_json(data / "state.json") or {}
    state.setdefault("houses", {})
    state.setdefault("name_misses", {})
    items = read_json(data / "items.json") or {}

    all_houses = list_houses(api, namespace, ruleset)
    selected, auto = choose_houses(cfg, all_houses)
    if not selected:
        raise ApiError("None of the auction houses in the index match the "
                       f"configured factions. Index: {all_houses}")

    # Discovery downloads every candidate house, so its answer is reused for a
    # day before checking again.
    found = state.get("discovered")
    rediscover = auto
    if auto and found and now - found.get("at", 0) < 86400:
        known = {h["id"]: h for h in all_houses}
        cached = [known[i] for i in found.get("ids", []) if i in known]
        if cached:
            selected, rediscover = cached, False

    fetched = []
    for house in selected:
        try:
            payload = fetch_house(api, namespace, ruleset, house["id"])
        except NotFound:
            log(f"house {house['id']}: not found, skipped")
            continue
        fetched.append((house, payload))

    if rediscover:
        # Every ruleset currently lists every house. If the house says which
        # ruleset it belongs to, keep only ours; otherwise track every
        # non-empty candidate until one is pinned in config.json.
        kept = []
        for faction in {faction_of(h["name"]) for h, _ in fetched}:
            group = [(h, p) for h, p in fetched if faction_of(h["name"]) == faction]
            ours = [(h, p) for h, p in group if p["ruleset"] == ruleset]
            if ours and len(ours) < len(group):
                group = ours
            kept.extend((h, p) for h, p in group
                        if p["auctions"] or p["commodities"]
                        or str(h["id"]) in state["houses"])
        fetched = sorted(kept, key=lambda hp: hp[0]["id"])
        state["discovered"] = {"ids": [h["id"] for h, _ in fetched], "at": now}
    elif not auto:
        state.pop("discovered", None)

    listing_counts: dict[int, int] = defaultdict(int)
    meta_houses = []
    wrote = 0
    for house, payload in fetched:
        hid = house["id"]
        house_dir = data / str(hid)
        hs = state["houses"].setdefault(str(hid), {"snapshots": 0, "rolled": []})
        stats, skipped = aggregate(payload["auctions"], payload["commodities"])

        modified = payload["last_modified"]
        ts = modified if modified and modified <= now + 300 else now
        last_ts = hs.get("last_ts", 0)
        if modified and modified == hs.get("last_modified"):
            fresh = False                       # Blizzard has not refreshed yet
        elif modified:
            fresh = ts > last_ts
        else:
            fresh = ts - last_ts >= MIN_GAP
        if not stats:
            fresh = False                       # empty response: keep old data

        if hs.get("sales_method") != SALES_METHOD:
            if hs.get("sales_since"):
                reset_sales(house_dir)          # figures from an older method
            hs["sales_method"] = SALES_METHOD
            hs["sales_since"] = None
        if fresh:
            book = open_book(payload["auctions"], payload["commodities"])
            prev = read_book(house_dir)
            sold = {}
            if prev and prev.get("ts") == last_ts and last_ts:
                sold = estimate_sold(prev, book, ts - last_ts)
                hs["sales_since"] = hs.get("sales_since") or ts
            write_book(house_dir, ts, book)
            append_hourly(house_dir, ts, stats, sold)
            hs["last_ts"] = ts
            hs["last_modified"] = modified
            hs["snapshots"] = hs.get("snapshots", 0) + 1
            hs.setdefault("first_ts", ts)
            wrote += 1
        roll_and_prune(house_dir, hs, now)
        if stats and (fresh or not (house_dir / "summary.json").exists()):
            rows, hours = build_summary(house_dir, hs.get("last_ts", ts), stats,
                                        hs.get("sales_since"))
            write_json(house_dir / "summary.json", {
                "house": hid, "updated": hs.get("last_ts", ts),
                "cols": SUMMARY_COLS, "hours": hours, "rows": rows})
        for item_id, row in stats.items():
            listing_counts[item_id] += row[4]

        faction = faction_of(house["name"])
        meta_houses.append({
            "id": hid, "name": house["name"], "faction": faction,
            "cut": cuts.get(faction, 0.05), "ruleset": payload["ruleset"],
            "snapshot": hs.get("last_ts"), "first": hs.get("first_ts"),
            "snapshots": hs.get("snapshots", 0),
            "sales_since": hs.get("sales_since"),
            "listings": len(payload["auctions"]) + len(payload["commodities"]),
            "items": len(stats)})
        log(f"house {hid} ({house['name']}): {len(stats)} items, "
            f"{meta_houses[-1]['listings']} listings, {skipped} skipped, "
            f"{'new snapshot' if fresh else 'no new snapshot'}")

    if not meta_houses:
        raise ApiError("No auction house returned any data.")

    if not auto:
        # Houses dropped from an explicit list take their data with them.
        tracked = {str(h["id"]) for h in meta_houses}
        for path in data.iterdir() if data.is_dir() else []:
            if path.is_dir() and path.name.isdigit() and path.name not in tracked:
                shutil.rmtree(path, ignore_errors=True)
                state["houses"].pop(path.name, None)

    wanted = sorted(listing_counts, key=lambda i: -listing_counts[i])
    resolve_names(api, region, items, state["name_misses"], wanted, now,
                  int(cfg.get("max_name_lookups", MAX_NAME_LOOKUPS)))
    state["name_misses"] = {k: v for k, v in state["name_misses"].items()
                            if v > now - 30 * 86400}

    try:
        ruleset_name = text(api.get(f"/data/wow/ruleset/{ruleset}", namespace)[0].get("name"))
    except (NotFound, ApiError):
        ruleset_name = ""

    meta = {
        "updated": now, "region": region,
        "ruleset": {"id": ruleset, "name": ruleset_name},
        "auto": auto, "houses": meta_houses, "all_houses": all_houses,
        "hourly_days": HOURLY_DAYS, "shards": SHARDS,
        "names": {"known": sum(1 for i in wanted if str(i) in items),
                  "missing": sum(1 for i in wanted if str(i) not in items)}}
    write_json(data / "items.json", items)
    write_json(data / "state.json", state)
    write_json(data / "meta.json", meta)
    log(f"done: {wrote} new snapshot(s), {len(items)} named items")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="public", help="site directory to update")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args()

    cfg = read_json(Path(args.config))
    if cfg is None:
        print(f"error: cannot read config {args.config}", file=sys.stderr)
        return 2
    client_id = os.environ.get("BLIZZARD_CLIENT_ID", "").strip()
    client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("error: BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET are not set. "
              "Add both under Settings > Secrets and variables > Actions.",
              file=sys.stderr)
        return 2
    try:
        api = Api(cfg.get("region", "eu"), client_id, client_secret)
        run(api, Path(args.out), cfg)
    except ApiError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
