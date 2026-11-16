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
                "points7d", "low30dMin", "low30dMed", "low30dMax", "points30d"]


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


# -------------------------------------------------------------- storage ----

def append_hourly(house_dir: Path, ts: int, stats: dict) -> None:
    """Add one snapshot to today's hourly files.

    File shape: {"t": [timestamps], "i": {id: [tIndex, low, market, qty, ...]}}
    """
    day_dir = house_dir / "h" / utc_day(ts)
    by_shard: dict[int, dict] = defaultdict(dict)
    for item_id, row in stats.items():
        by_shard[item_id % SHARDS][item_id] = row
    for shard, rows in by_shard.items():
        path = day_dir / f"{shard:02d}.json"
        doc = read_json(path) or {"t": [], "i": {}}
        idx = len(doc["t"])
        doc["t"].append(ts)
        for item_id, row in rows.items():
            doc["i"].setdefault(str(item_id), []).extend([idx, row[0], row[1], row[2]])
        write_json(path, doc)


def load_shard_history(house_dir: Path, shard: int, days: list[str]) -> dict:
    """{item id: [(ts, low, market, qty), ...]} for one shard, oldest first."""
    hist: dict[int, list] = defaultdict(list)
    for day in days:
        doc = read_json(house_dir / "h" / day / f"{shard:02d}.json")
        if not doc:
            continue
        times = doc["t"]
        for key, flat in doc["i"].items():
            points = hist[int(key)]
            for k in range(0, len(flat) - 3, 4):
                points.append((times[flat[k]], flat[k + 1], flat[k + 2], flat[k + 3]))
    return hist


def hourly_days(house_dir: Path) -> list[str]:
    root = house_dir / "h"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))


def roll_day(house_dir: Path, day: str) -> None:
    """Fold one finished UTC day into the monthly daily files.

    File shape: {"i": {id: [dayOfMonth, low, high, market, qty, hours, ...]}}
    """
    month, dom = day[:7], int(day[8:])
    for shard in range(SHARDS):
        doc = read_json(house_dir / "h" / day / f"{shard:02d}.json")
        if not doc:
            continue
        path = house_dir / "d" / month / f"{shard:02d}.json"
        monthly = read_json(path) or {"i": {}}
        for key, flat in doc["i"].items():
            lows, markets, qtys = flat[1::4], flat[2::4], flat[3::4]
            if not lows:
                continue
            old = monthly["i"].get(key, [])
            kept = []
            for k in range(0, len(old) - 5, 6):
                if old[k] != dom:
                    kept.extend(old[k:k + 6])
            kept.extend([dom, min(lows), max(lows), median(markets),
                         round(sum(qtys) / len(qtys)), len(lows)])
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


def build_summary(house_dir: Path, ts: int, stats: dict) -> list[list]:
    """One row per item listed right now, with its recent range (SUMMARY_COLS)."""
    days = [d for d in hourly_days(house_dir) if d >= utc_day(ts - HOURLY_DAYS * 86400)]
    by_shard: dict[int, list] = defaultdict(list)
    for item_id in stats:
        by_shard[item_id % SHARDS].append(item_id)
    rows = []
    for shard, ids in by_shard.items():
        hist = load_shard_history(house_dir, shard, days)
        for item_id in ids:
            points = hist.get(item_id) or [(ts, *stats[item_id][:3])]
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
                         min(lows30), median(lows30), max(lows30), len(w30)])
    rows.sort(key=lambda r: r[0])
    return rows


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

        if fresh:
            append_hourly(house_dir, ts, stats)
            hs["last_ts"] = ts
            hs["last_modified"] = modified
            hs["snapshots"] = hs.get("snapshots", 0) + 1
            hs.setdefault("first_ts", ts)
            wrote += 1
        roll_and_prune(house_dir, hs, now)
        if stats and (fresh or not (house_dir / "summary.json").exists()):
            write_json(house_dir / "summary.json", {
                "house": hid, "updated": hs.get("last_ts", ts),
                "cols": SUMMARY_COLS,
                "rows": build_summary(house_dir, hs.get("last_ts", ts), stats)})
        for item_id, row in stats.items():
            listing_counts[item_id] += row[4]

        faction = faction_of(house["name"])
        meta_houses.append({
            "id": hid, "name": house["name"], "faction": faction,
            "cut": cuts.get(faction, 0.05), "ruleset": payload["ruleset"],
            "snapshot": hs.get("last_ts"), "first": hs.get("first_ts"),
            "snapshots": hs.get("snapshots", 0),
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
