#!/usr/bin/env python3
"""Run the collector against a fake Forever API.

The fake mirrors what is known about the undocumented API: rulesets instead of
realms, 15 auction houses listed under every ruleset, empty houses with no
"auctions" field, bid-only auctions, commodities under each house, and item
lookups that 404 for a large share of ids.

    python3 tests/simulate.py                 run the checks
    python3 tests/simulate.py --out DIR       also leave a data set in DIR
                                              (for previewing the dashboard)
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import tempfile
import time
from email.utils import formatdate
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import collect  # noqa: E402

HOUSES = {}
for _id in range(110, 115):
    HOUSES[_id] = "Alliance Auction House"
for _id in range(115, 120):
    HOUSES[_id] = "Horde Auction House"
for _id in (120, 121, 123, 124, 125):
    HOUSES[_id] = "Blackwater Auction House"
EMPTY = {112, 115, 123}
HOUSE_RULESET = {110: 1, 111: 2, 113: 3, 114: 15, 116: 1, 117: 2, 118: 3, 119: 15,
                 120: 1, 121: 2, 124: 3, 125: 15, 112: 9, 115: 9, 123: 9}

GOODS = ["Copper Ore", "Tin Ore", "Iron Ore", "Mithril Ore", "Thorium Ore",
         "Linen Cloth", "Wool Cloth", "Silk Cloth", "Mageweave Cloth", "Runecloth",
         "Light Leather", "Medium Leather", "Heavy Leather", "Thick Leather",
         "Rugged Leather", "Peacebloom", "Silverleaf", "Earthroot", "Briarthorn",
         "Kingsblood", "Fadeleaf", "Goldthorn", "Sungrass", "Dreamfoil",
         "Black Lotus", "Strange Dust", "Soul Dust", "Vision Dust", "Dream Dust",
         "Illusion Dust", "Greater Magic Essence", "Large Brilliant Shard",
         "Swiftthistle", "Deadly Poison", "Instant Poison", "Flash Powder",
         "Thistle Tea", "Elixir of the Mongoose", "Major Healing Potion",
         "Arcanite Bar", "Thorium Bar", "Devilsaur Leather", "Essence of Air"]
ADJ = ["Bandit", "Sentry", "Raider's", "Scouting", "Dervish", "Ranger", "Glimmering",
       "Battleforge", "Nightsky", "Thistlefur", "Imposing", "Heraldic", "Wicked"]
NOUN = ["Gloves", "Boots", "Cloak", "Belt", "Dagger", "Shortsword", "Cuirass",
        "Bracers", "Pants", "Shoulderpads", "Buckler", "Mace"]
SUFFIX = ["of the Monkey", "of the Tiger", "of Agility", "of the Bear", "of Stamina"]


def item_name(item_id: int) -> str:
    if item_id - 700 < len(GOODS):
        return GOODS[item_id - 700]
    rng = random.Random(item_id)
    return f"{rng.choice(ADJ)} {rng.choice(NOUN)} {rng.choice(SUFFIX)}"


class FakeApi:
    """Stands in for collect.Api; `now` is advanced by the simulation."""

    def __init__(self, ruleset_info=True, items_per_house=320):
        self.now = 0
        self.ruleset_info = ruleset_info
        self.items_per_house = items_per_house
        self.extra: dict[int, list] = {}      # house -> injected auctions
        self.calls = 0
        self.auction_calls = 0

    # -- market model ----------------------------------------------------
    def _market(self, house: int):
        """Deterministic prices for one house at self.now."""
        hour = self.now // 3600
        count = self.items_per_house if HOUSE_RULESET[house] == 1 else 60
        if "Blackwater" in HOUSES[house]:
            count //= 6
        auctions, commodities = [], []
        auction_id = house * 10_000_000
        for item_id in range(700, 700 + count):
            base = random.Random(item_id * 31 + house)
            price = math.exp(base.uniform(math.log(40), math.log(900_000)))
            drift = 1 + 0.25 * math.sin(hour / 37 + item_id) \
                      + 0.08 * math.sin(hour * 2 * math.pi / 24 + item_id)
            rng = random.Random(item_id * 7919 + house * 13 + hour)
            if rng.random() < 0.12:            # not listed this hour
                continue
            commodity = item_id - 700 < len(GOODS)
            for _ in range(rng.randint(1, 12 if commodity else 3)):
                unit = max(1, int(price * drift * rng.uniform(1.0, 1.6)))
                qty = rng.randint(1, 20) if commodity else 1
                auction_id += 1
                if commodity:
                    commodities.append({"id": auction_id, "item": {"id": item_id},
                                        "quantity": qty, "unit_price": unit,
                                        "time_left": "LONG"})
                else:
                    entry = {"id": auction_id,
                             "item": {"id": item_id, "context": 0,
                                      "bonus_lists": [], "modifiers": []},
                             "bid": unit // 2, "quantity": qty, "time_left": "SHORT"}
                    if rng.random() > 0.04:     # a few are bid-only
                        entry["buyout"] = unit * qty
                    auctions.append(entry)
        return auctions + self.extra.get(house, []), commodities

    # -- API surface -------------------------------------------------------
    def get(self, path, namespace, tries=4):
        self.calls += 1
        headers = {"Last-Modified": formatdate(self.now - self.now % 3600, usegmt=True)}
        parts = path.strip("/").split("/")
        if namespace.startswith("static-"):
            return self._item(int(parts[-1]), namespace), headers
        self.auction_calls += 1
        assert namespace == "dynamic-forever-eu", namespace
        if path.endswith("/auctions"):
            raise collect.NotFound(path)             # index needs the slash
        if path.endswith("/auctions/"):
            return {"auctions": [{"key": {"href": "x"}, "name": name, "id": hid}
                                 for hid, name in HOUSES.items()]}, headers
        if parts[-1] == "commodities":
            house = int(parts[-2])
            if house in EMPTY:
                raise collect.NotFound(path)
            return {"auctions": self._market(house)[1]}, headers
        if parts[-2] == "auctions":
            house = int(parts[-1])
            if house not in HOUSES:
                raise collect.NotFound(path)
            doc = {"id": house, "name": HOUSES[house], "commodities": {"href": "x"}}
            if self.ruleset_info:
                rid = HOUSE_RULESET[house]
                doc["ruleset"] = {"key": {"href": f"https://eu.api.blizzard.com/data/wow/ruleset/{rid}?namespace=dynamic-forever-eu"}}
            if house not in EMPTY:
                doc["auctions"] = self._market(house)[0]
            return doc, headers
        if parts[-2] == "ruleset":
            return {"id": int(parts[-1]), "name": "PvP", "status": "UP"}, headers
        raise collect.NotFound(path)

    def get_href(self, href, tries=4):
        raise AssertionError("not used by the fake")

    def _item(self, item_id, namespace):
        if item_id % 50 == 0:
            raise collect.NotFound("unknown everywhere")
        in_forever = item_id % 7 < 4
        if namespace.startswith("static-forever") and not in_forever:
            raise collect.NotFound("not in forever")
        if namespace.startswith("static-classic-"):
            raise collect.NotFound("not in classic")
        rng = random.Random(item_id)
        name = item_name(item_id)
        commodity = item_id - 700 < len(GOODS)
        quality = "COMMON" if commodity else rng.choice(["UNCOMMON", "UNCOMMON", "RARE", "EPIC"])
        return {"id": item_id,
                "name": name if in_forever else {"en_US": name, "de_DE": "x"},
                "quality": {"type": quality, "name": quality.title()},
                "level": rng.randint(5, 60),
                "item_class": {"name": "Trade Goods" if commodity else "Armor", "id": 7},
                "item_subclass": {"name": "Herb" if commodity else "Leather", "id": 1},
                "sell_price": rng.randint(5, 4000)}


CFG = {"region": "eu", "ruleset": 1, "factions": ["horde", "blackwater"],
       "houses": "auto", "auction_cut": {"horde": 0.05, "blackwater": 0.15}}


def simulate(out: Path, days: float, end: int, ruleset_info=True, cfg=None,
             inject=True) -> tuple[FakeApi, int]:
    """Hourly runs ending at `end`. Returns the fake and the number of
    distinct hours that produced a snapshot."""
    api = FakeApi(ruleset_info=ruleset_info)
    cfg = cfg or CFG
    hours = int(days * 24)
    rng = random.Random(42)
    skipped = set(rng.sample(range(5, hours - 5), 4)) if hours > 20 else set()
    taken = 0
    for step in range(hours, -1, -1):
        if step in skipped:
            continue                                    # a missed cron run
        api.now = end - step * 3600 + 420               # cron fires at :07
        if step == 0 and inject:
            # One clear deal and one below-vendor listing for the dashboard.
            api.extra[116] = [
                {"id": 1, "item": {"id": 760}, "quantity": 1, "time_left": "LONG",
                 "buyout": max(1, deal_price(api, 760) // 2)},
                {"id": 2, "item": {"id": 771}, "quantity": 1, "time_left": "LONG",
                 "buyout": 3}]
        collect.run(api, out, cfg, now=api.now)
        taken += 1
        if step % 9 == 0:                               # a duplicate manual run
            api.now += 600
            collect.run(api, out, cfg, now=api.now)
    return api, taken


def deal_price(api: FakeApi, item_id: int) -> int:
    # The item is not listed every hour, so look back until it is.
    now = api.now
    try:
        for back in range(48):
            api.now = now - back * 3600
            rows = [a["buyout"] for a in api._market(116)[0]
                    if a["item"]["id"] == item_id and "buyout" in a]
            if rows:
                return min(rows)
    finally:
        api.now = now
    return 50


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        check.failed += 1


check.failed = 0


def run_checks(keep: Path | None, days: float) -> int:
    end = int(time.time()) // 3600 * 3600
    collect.log = lambda msg: None

    print("1. Houses that name their ruleset")
    out = keep or Path(tempfile.mkdtemp())
    api, taken = simulate(out, days, end)
    data = out / "data"
    meta = collect.read_json(data / "meta.json")
    ids = [h["id"] for h in meta["houses"]]
    check(ids == [116, 120], f"tracks one Horde and one Blackwater house: {ids}")
    horde = meta["houses"][0]
    check(horde["snapshots"] == taken, f"one snapshot per distinct hour ({horde['snapshots']} of {taken})")
    day_dirs = collect.hourly_days(data / "116")
    check(len(day_dirs) >= int(days), f"hourly folders per day: {len(day_dirs)}")
    state = collect.read_json(data / "state.json")
    rolled = state["houses"]["116"]["rolled"]
    check(set(rolled) == set(day_dirs[:-1]), "every finished day rolled into daily files")
    monthly = list((data / "116" / "d").glob("*/*.json"))
    check(bool(monthly), f"daily files written: {len(monthly)}")
    sample = collect.read_json(monthly[0])
    lens = {len(v) % 7 for v in sample["i"].values()}
    check(lens == {0}, "daily rows are 7 numbers each")

    summary = collect.read_json(data / "116" / "summary.json")
    cols = summary["cols"]
    rows = {r[0]: dict(zip(cols, r)) for r in summary["rows"]}
    check(all(len(r) == len(cols) for r in summary["rows"]), "summary rows match the header")
    check(all(r["low7dMin"] <= r["low"] <= r["low7dMax"] for r in rows.values()),
          "current low sits inside its 7-day range")
    check(all(r["low"] <= r["market"] for r in rows.values()), "market price is never below the low")
    deal = rows.get(760)
    check(bool(deal) and deal["low"] < 0.62 * deal["low7dMed"],
          f"injected deal stands out: {deal and (deal['low'], deal['low7dMed'])}")
    items = collect.read_json(data / "items.json")
    flip = rows.get(771)
    check(bool(flip) and flip["low"] == 3 and items["771"][5] > 3, "injected vendor flip is visible")
    check(sum(1 for r in rows.values() if r["points7d"] > 100) > 100, "items carry a week of points")
    check(sum(1 for r in rows.values() if r["low24hAgo"]) > 100, "24-hour comparison filled in")

    check(items["701"][0] == "Tin Ore" and items["701"][6] == 0, "name from the Forever namespace")
    fallback = [v for k, v in items.items() if int(k) % 7 >= 4]
    check(bool(fallback) and all(v[6] == 1 and isinstance(v[0], str) for v in fallback),
          "names fall back to the Classic Era namespace")
    check("750" not in items and "750" in state["name_misses"], "unknown items are parked for retry")
    used = []
    for _ in range(2):          # at most one of two runs re-checks the houses
        calls = api.auction_calls
        api.now += 3600
        collect.run(api, out, CFG, now=api.now)
        used.append(api.auction_calls - calls)
    used = min(used)
    check(used <= 6, f"a normal run only downloads the tracked houses ({used} requests)")

    print("2. Bid-only and malformed listings")
    stats, skipped = collect.aggregate(
        [{"item": {"id": 5}, "quantity": 2, "buyout": 300},
         {"item": {"id": 5}, "quantity": 1, "bid": 10},
         {"item": 6, "quantity": 1, "buyout": 0}, "junk", {"quantity": 1, "buyout": 5}],
        [{"item": {"id": 9}, "quantity": 10, "unit_price": 40},
         {"item": {"id": 9}, "quantity": 90, "unit_price": 100}])
    check(stats[5] == [150, 150, 2, 2, 1] and skipped == 4, f"stack buyout becomes a unit price: {stats.get(5)}")
    check(stats[9] == [40, 60, 100, 10, 2], f"market = cheapest 15% of units: {stats.get(9)}")

    print("2b. Estimated sales")
    # auctions: 1 and 3 vanish with time left, 2 vanishes near expiry, 4 stays.
    # commodities of item 8: 9 is partly bought, 10 (the cheapest) vanishes,
    # 11 vanishes although the cheaper 9 is still there, so it was cancelled.
    prev = {"a": {"1": [5, 1, 2, 100], "2": [5, 1, 0, 100], "3": [6, 1, 1, 50], "4": [7, 1, 2, 70]},
            "c": {"9": [8, 20, 2, 10], "10": [8, 5, 1, 9], "11": [8, 50, 2, 30]}}
    cur = {"a": {"4": [7, 1, 2, 70]}, "c": {"9": [8, 12, 2, 10]}}
    got = collect.estimate_sold(prev, cur, 3600)
    check(got == {5: 1, 6: 1, 8: 13}, f"vanished cheap listings and partial buys count, dearer ones do not: {got}")
    check(collect.estimate_sold(prev, cur, 5 * 3600) == {5: 1, 8: 8},
          "longer gap: only listings with more than 8 hours left count")
    check(collect.estimate_sold(prev, cur, 12 * 3600) == {8: 8}, "very long gap: only partial buys count")
    relist = {"a": {"4": [7, 1, 2, 70], "20": [5, 1, 2, 95]}, "c": {"9": [8, 12, 2, 10]}}
    check(collect.estimate_sold(prev, relist, 3600) == {6: 1, 8: 13}, "a reposted one-off item is not a sale")
    check(collect.estimate_sold({"a": {"1": [5, 1, 2]}, "c": {}}, {"a": {}, "c": {}}, 3600) == {5: 1},
          "books saved before prices were recorded still work")
    book = collect.open_book([{"id": 1, "item": {"id": 5}, "quantity": 2, "buyout": 18, "time_left": "VERY_LONG"},
                              {"id": 2, "item": {"id": 5}, "quantity": 1, "bid": 9, "time_left": "LONG"}],
                             [{"id": 3, "item": {"id": 8}, "quantity": 4, "unit_price": 2, "time_left": "SHORT"}])
    check(book == {"a": {"1": [5, 2, 2, 9]}, "c": {"3": [8, 4, 0, 2]}}, "bid-only listings stay out of the book")
    check(len(cols) == 25 and all(r["sold1d"] >= 0 and r["sold7d"] >= r["sold1d"] for r in rows.values()),
          "summary carries 24-hour and 7-day sales")
    check(sum(r["sold7d"] for r in rows.values()) > 0, "simulated run produced sales")
    check((data / "116" / "open.json.gz").exists() and bool(meta["houses"][0]["sales_since"]),
          "last snapshot's listings saved for the next comparison")
    hdoc = collect.read_json(sorted((data / "116" / "h").glob("*/*.json"))[-1])
    check(hdoc["n"] == 5 and all(len(v) % 5 == 0 for v in hdoc["i"].values()), "hourly points are 5 numbers each")
    old_doc = {"t": [1, 2], "i": {"7": [0, 10, 11, 3, 1, 12, 13, 4]}}
    check(collect.widen(old_doc, 4, 5)["i"]["7"] == [0, 10, 11, 3, 0, 1, 12, 13, 4, 0], "older 4-number files are upgraded")

    print("2c. Hour-of-day profiles")
    timed = [r for r in rows.values() if r["buyHour"] is not None]
    check(len(timed) > 150, f"items get a best hour to buy and sell: {len(timed)}")
    check(all(0 <= r["buyHour"] < 24 and 0 <= r["sellHour"] < 24 and r["buyPct"] >= 0 and r["sellPct"] >= 0
              for r in timed), "best hours are valid and on the right side of the daily average")
    # the simulation prices every item 8% up and down over the day, so the
    # typical gap between an item's dearest and cheapest hour is well above zero
    swing = sorted(r["buyPct"] + r["sellPct"] for r in timed)[len(timed) // 2]
    check(8 < swing < 30, f"typical daily swing matches the simulated rhythm: {swing:.1f}%")
    check(any(r["peakHour"] is not None for r in timed), "busiest selling hour found")
    hours = summary["hours"]
    check(len(hours["traded"]) == 24 and sum(hours["traded"]) > 0 and min(hours["snapshots"]) >= 7,
          "market-wide gold traded by hour")
    check(all(v is not None and 0.8 < v < 1.25 for v in hours["price"]), "market-wide price level by hour")
    prof = collect.read_json(data / "116" / "p" / "07.json")["i"]
    check(bool(prof) and all(len(v) == 49 for v in prof.values()), "per-item hour profiles written")
    pr, cn, sd = collect.hour_profile(
        [(d * 86400 + h * 3600 + 1140, 100 + (20 if h == 20 else -20 if h == 4 else 0), 100, 5, 3 if h == 21 else 0)
         for d in range(4) for h in range(24)], [4] * 24, 0)
    check(collect.best_hours(pr, cn, sd, [4] * 24) == [4, 10.0, 20, 10.0, 20, 4],
          f"cheapest, dearest and busiest hour picked out: {collect.best_hours(pr, cn, sd, [4] * 24)}")
    check(collect.best_hours(*collect.hour_profile(
        [(h * 3600, 100, 100, 5, 0) for h in range(24)], [1] * 24, 0), [1] * 24)[:5] == [None] * 5,
          "one day of history names no best hour")

    print("3. Pruning old hourly data")
    old = collect.HOURLY_DAYS
    collect.HOURLY_DAYS = 3
    api.now += 3600
    collect.run(api, out, CFG, now=api.now)
    left = collect.hourly_days(data / "116")
    check(len(left) <= 4, f"hourly folders trimmed to the window: {len(left)}")
    kept_days = {v[k] for v in collect.read_json(monthly[0])["i"].values()
                 for k in range(0, len(v), 7)}
    check(len(kept_days) >= min(int(days) - 1, 1), "daily history survives pruning")
    collect.HOURLY_DAYS = old

    print("4. Houses that do not name their ruleset")
    out2 = Path(tempfile.mkdtemp())
    simulate(out2, 0.2, end, ruleset_info=False, inject=False)
    meta2 = collect.read_json(out2 / "data" / "meta.json")
    ids2 = [h["id"] for h in meta2["houses"]]
    check(ids2 == [116, 117, 118, 119, 120, 121, 124, 125],
          f"tracks every non-empty candidate: {ids2}")

    print("5. Pinning houses in config.json")
    pinned = dict(CFG, houses=[117, 121])
    api2 = FakeApi(ruleset_info=False)
    api2.now = end + 7200
    collect.run(api2, out2, pinned, now=api2.now)
    dirs = sorted(p.name for p in (out2 / "data").iterdir() if p.is_dir())
    check(dirs == ["117", "121"], f"untracked house data removed: {dirs}")

    print("\nall checks passed" if not check.failed else f"\n{check.failed} check(s) FAILED")
    return 1 if check.failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="keep the simulated data set here")
    parser.add_argument("--days", type=float, default=9)
    args = parser.parse_args()
    if args.out:
        target = Path(args.out)
        collect.log = lambda msg: None
        simulate(target, args.days, int(time.time()) // 3600 * 3600)
        print(f"wrote {target}/data")
        sys.exit(0)
    sys.exit(run_checks(None, args.days))
