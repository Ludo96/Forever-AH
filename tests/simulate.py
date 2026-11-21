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
    check(len(cols) == 27 and all(r["sold1d"] >= 0 and r["sold7d"] >= r["sold1d"] for r in rows.values()),
          "summary carries 24-hour and 7-day sales")
    check(sum(r["sold7d"] for r in rows.values()) > 0, "simulated run produced sales")
    check((data / "116" / "open.json.gz").exists() and bool(meta["houses"][0]["sales_since"]),
          "last snapshot's listings saved for the next comparison")
    hdoc = collect.read_json(sorted((data / "116" / "h").glob("*/*.json"))[-1])
    check(hdoc["n"] == 6 and all(len(v) % 6 == 0 for v in hdoc["i"].values()), "hourly points are 6 numbers each")
    paid = {}
    collect.estimate_sold(prev, cur, 3600, paid)
    check(paid == {5: 100, 6: 50, 8: round((8 * 10 + 5 * 9) / 13)}, f"sold units carry the price they went for: {paid}")
    paid = {}
    collect.estimate_sold({"a": {"1": [5, 1, 2, 100], "2": [5, 1, 2, 102], "3": [5, 1, 2, 104]}, "c": {}},
                          {"a": {"9": [5, 1, 2, 99]}, "c": {}}, 3600, paid)
    check(paid == {5: 102}, f"reposts are netted out of the price too: {paid}")
    check(collect.last_sale([(100, 5, 5, 1, 2, 40), (200, 5, 5, 1, 0, 0), (90000, 5, 5, 1, 6, 80), (90500, 5, 5, 1, 2, 100)], 91000)
          == [85, 90500], "last sale price averages the past 24 hours of sales")
    check(collect.last_sale([(100, 5, 5, 1, 2, 40)], 500000) == [40, 100] and collect.last_sale([(1, 5, 5, 1, 3, 0)], 9) == [None, None],
          "an older sale is used when nothing sold today; sales without a price are ignored")
    priced = [r for r in rows.values() if r["soldPrice"]]
    check(len(priced) > 10 and all(r["soldAt"] <= summary["updated"] for r in priced), f"summary carries sale prices: {len(priced)}")
    old_doc = {"t": [1, 2], "i": {"7": [0, 10, 11, 3, 1, 12, 13, 4]}}
    check(collect.widen(old_doc, 4, 6)["i"]["7"] == [0, 10, 11, 3, 0, 0, 1, 12, 13, 4, 0, 0], "older 4-number files are upgraded")
    five = {"n": 5, "t": [1], "i": {"7": [0, 10, 11, 3, 2]}}
    check(collect.widen(five, 4, 6)["i"]["7"] == [0, 10, 11, 3, 2, 0], "5-number files keep their sales when upgraded")

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

    print("6. Recipes from Blizzard's profession data")
    import recipes

    class RecipeApi:
        docs = {
            "/data/wow/profession/index": {"professions": [{"name": "Leatherworking", "id": 165}, {"name": "Enchanting", "id": 333}]},
            "/data/wow/profession/165": {"skill_tiers": [{"name": "Leatherworking", "id": 2945}]},
            "/data/wow/profession/333": {"skill_tiers": [{"name": "Enchanting", "id": 2946}]},
            "/data/wow/profession/165/skill-tier/2945": {"categories": [{"name": "Armor Kits", "recipes": [
                {"name": "Thick Armor Kit", "id": 5667}, {"name": "Mystic Thick Armor Kit", "id": 55135},
                {"name": "New Trainer Kit", "id": 60000}, {"name": "Gone", "id": 60001},
                {"name": "Trainer Only A", "id": 60002}, {"name": "Trainer Only B", "id": 60003}]}]},
            "/data/wow/profession/333/skill-tier/2946": {"categories": [{"name": "Enchant", "recipes": [{"name": "Enchant Chest", "id": 7000}]}]},
            "/data/wow/recipe/5667": {"crafted_item": {"name": "Thick Armor Kit", "id": 8173}, "crafted_quantity": {"value": 1.0},
                "reagents": [{"reagent": {"name": "Thick Leather", "id": 4304}, "quantity": 5}, {"reagent": {"name": "Silken Thread", "id": 4291}, "quantity": 1}]},
            "/data/wow/recipe/55135": {"crafted_item": {"name": "Mystic Thick Armor Kit", "id": 252462}, "crafted_quantity": {"minimum": 1, "maximum": 3},
                "reagents": [{"reagent": {"name": "Thick Armor Kit", "id": 8173}, "quantity": 1}, {"reagent": {"name": "Vision Dust", "id": 11137}, "quantity": 2}]},
            "/data/wow/recipe/60000": {"crafted_item": {"name": "New Trainer Kit", "id": 260000}, "crafted_quantity": {"value": 1.0},
                "reagents": [{"reagent": {"name": "Pristine Hide", "id": 249428}, "quantity": 2}]},
            "/data/wow/recipe/60002": {"crafted_item": {"name": "Trainer Only A", "id": 260002}, "crafted_quantity": {"value": 1.0},
                "reagents": [{"reagent": {"name": "Pristine Hide", "id": 249428}, "quantity": 1}]},
            "/data/wow/recipe/60003": {"crafted_item": {"name": "Trainer Only B", "id": 260003}, "crafted_quantity": {"value": 1.0},
                "reagents": [{"reagent": {"name": "Pristine Hide", "id": 249428}, "quantity": 1}]},
            "/data/wow/recipe/7000": {"name": "Enchant Chest", "reagents": [{"reagent": {"name": "Strange Dust", "id": 10940}, "quantity": 1}]},
            "/data/wow/item/252999": {"preview_item": {"requirements": {"skill": {"profession": {"name": "Leatherworking", "id": 165}, "level": 210}},
                "recipe": {"item": {"item": {"id": 252462}}}}},
            "/data/wow/item/14000": {"preview_item": {}},
            "/data/wow/item/253000": {"preview_item": {"requirements": {"skill": {"profession": {"name": "Leatherworking", "id": 165}, "level": 290}},
                "recipe": {"item": {"item": {"id": 260000}}}}},
        }

        def get(self, path, namespace, tries=4):
            if path not in self.docs:
                raise collect.NotFound(path)
            return self.docs[path], {}

        def search(self, path, params, namespace):       # one recipe item not seen on the AH
            first = params["id"] == "[1,]"
            return {"results": [{"data": {"id": 253000}}] if first else []}, {}

    cache = {}
    built = recipes.build(RecipeApi(), "eu",
                          {"252999": ["Pattern: Mystic Thick Armor Kit", 2, "Recipe", "Leatherworking", 40, 0, 0],
                           "14000": ["Pattern: Something", 2, "Recipe", "Leatherworking", 40, 0, 0],
                           "8173": ["Thick Armor Kit", 1, "Consumable", "Permanent", 40, 1000, 0]},
                          {"skill": {"Leatherworking": {"8173": 200}}, "vendor": {"4291": 500}, "quality": {}}, cache, log=lambda m: None)
    lw = built["professions"].get("Leatherworking")
    check(list(built["professions"]) == ["Leatherworking"] and len(lw) == 5, "recipes that make no item are left out")
    check(lw[0] == [8173, 200, 1, 1, [4304, 5, 4291, 1]], f"old recipe takes its skill from Classic: {lw[0]}")
    check(lw[1] == [252462, 210, 1, 3, [8173, 1, 11137, 2]], f"new recipe takes its skill from the item that teaches it: {lw[1]}")
    check(lw[2] == [260000, 290, 1, 1, [249428, 2]], "recipe items found by search supply skill levels too")
    check(lw[3][1] is None and lw[4][1] is None, "a new trainer recipe has no skill level and sorts last")
    check(built["vendor"] == {"4291": 500} and built["names"]["252462"] == "Mystic Thick Armor Kit" and built["official"],
          "vendor prices and names carried over")
    check(cache == {"252999": [252462, "Leatherworking", 210], "14000": [], "253000": [260000, "Leatherworking", 290]},
          "recipe items are only read once")

    print("7. Telegram alerts")
    import alerts
    check([alerts.parse_price(x) for x in ("1g 50s", "1g50s", "18s", "2500c", "1.5g", "3", "2g 5s 7c", "abc", "5x", "")]
          == [15000, 15000, 1800, 2500, 15000, 30000, 20507, None, None, None], "prices are read in every common form")
    check(alerts.money(20507) == "2g 5s 7c" and alerts.money(1800) == "18s" and alerts.money(10000) == "1g"
          and alerts.money(10001) == "1g 0s 1c", "prices are written like the game does")
    sealed = alerts.seal({"offset": 5, "alerts": {"42": []}}, "tok")
    check(alerts.unseal(sealed, "tok") == {"offset": 5, "alerts": {"42": []}} and b"42" not in sealed
          and alerts.unseal(sealed, "other") is None, "alert storage is encrypted and tied to the bot token")

    class FakeBot:
        def __init__(self):
            self.sent, self.queue = [], []
        def updates(self, offset):
            return [u for u in self.queue if u["update_id"] >= offset]
        def send(self, chat, text):
            if chat == "666":
                raise alerts.TelegramError(403, "blocked")
            self.sent.append((chat, text))

    tdir = Path(tempfile.mkdtemp())
    (tdir / "data" / "113").mkdir(parents=True)
    collect.write_json(tdir / "data" / "items.json", {"12359": ["Thorium Bar", 1], "12360": ["Arcanite Bar", 1],
                                                       "2589": ["Linen Cloth", 1], "2592": ["Wool Cloth", 1]})
    def prices(low):
        collect.write_json(tdir / "data" / "113" / "summary.json",
                           {"cols": ["id", "low"], "rows": [[12359, low], [12360, 500000], [2589, 10]]})
    msg = lambda uid, chat, text: {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}
    bot = FakeBot()
    prices(1900)
    bot.queue = [msg(1, 42, "/alert thorium bar below 18s"), msg(2, 42, "/alert Arcanite Bar above 60g"),
                 msg(3, 42, "/alert cloth below 1s"), msg(4, 7, "/start a_2589_b_12"), msg(5, 7, "hello"),
                 msg(6, 666, "/alert Linen Cloth below 5c"), msg(7, 42, "/alert Thorium Bar below lots")]
    run = lambda: alerts.run(bot, tdir, "tok", "https://x/", log=lambda m: None)
    state = run()
    said = {u: t for u, (c, t) in enumerate(bot.sent)}
    check(len(bot.sent) == 6 and "Alert set: Thorium Bar below 18s" in bot.sent[0][1] and "19s" in bot.sent[0][1],
          f"alerts are confirmed with the current price: {bot.sent[:1]}")
    check("Which one?" in bot.sent[2][1] and "Linen Cloth" in bot.sent[2][1], "an unclear name lists the matches")
    check(bot.sent[3] == ("7", "Alert set: Linen Cloth below 12c. Lowest right now: 10c.") , f"dashboard link sets an alert: {bot.sent[3]}")
    check(any("Linen Cloth dropped to 10c" in t and "#h=113&t=all&i=2589" in t for c, t in bot.sent if c == "7"),
          "an alert that is already met fires straight away, with a link")
    check("could not read the price" in bot.sent[4][1], "a bad price is explained")
    check("666" not in state["alerts"] and state["offset"] == 8, "people who blocked the bot are dropped; messages are read once")
    bot.sent = []
    prices(1750)
    run()
    check(bot.sent == [("42", "Thorium Bar dropped to 17s 50c (your alert: below 18s).\nhttps://x/#h=113&t=all&i=12359")],
          f"crossing the level fires once: {bot.sent}")
    bot.sent = []
    prices(1790); run(); prices(1820); run(); prices(1700); run()
    check(bot.sent == [], "it stays quiet until the price has moved 3% back across")
    prices(1860); run(); prices(1780); state = run()
    check(len(bot.sent) == 1 and "17s 80c" in bot.sent[0][1], "and then fires again")
    bot.sent = []
    bot.queue = [msg(8, 42, "/list"), msg(9, 42, "/remove 1"), msg(10, 42, "/remove 9"), msg(11, 7, "/clear")]
    state = run()
    check("1. Thorium Bar below 18s" in bot.sent[0][1] and "2. Arcanite Bar above 60g" in bot.sent[0][1]
          and bot.sent[1][1] == "Removed: Thorium Bar below 18s." and "/list" in bot.sent[2][1]
          and "Removed all 1" in bot.sent[3][1] and list(state["alerts"]) == ["42"], "list, remove and clear work")
    check(b"Thorium" not in (tdir / "data" / "alerts.bin").read_bytes(), "nothing readable is published")

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
