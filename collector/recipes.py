#!/usr/bin/env python3
"""Build data/recipes.json from Blizzard's own Forever profession data.

    profession index -> skill tiers -> recipe ids -> recipe (item made,
    materials, amount made)

Blizzard's recipe records do not say what skill a recipe needs, so that comes
from two places: the recipe item that teaches it (Pattern, Plans, Formula...;
every one is found through Blizzard's item search and read once), and
otherwise the original Classic value for the same profession and item
(collector/classic.json). New trainer-taught recipes can therefore have no
skill level.

Runs after the collector and rebuilds at most once a day. Any failure leaves
the previous file in place and only warns, so prices keep publishing.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect import Api, ApiError, NotFound, read_json, text, write_json  # noqa: E402

REBUILD_AFTER = 24 * 3600
VERSION = 2                     # raise to force a rebuild after changing this script
RECIPE_CLASS = 9                # Blizzard's item class for Pattern, Plans, Formula...
MAX_ITEM_LOOKUPS = 2500         # recipe items read per run


def fetch_all(api, paths: list[str], namespace: str) -> tuple[dict, int]:
    """{path: doc} for every path that exists, and how many failed."""
    def one(path):
        try:
            return path, api.get(path, namespace, tries=3)[0]
        except NotFound:
            return path, None
        except ApiError:
            return path, False
    docs, failed = {}, 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for path, doc in pool.map(one, paths):
            if doc is False:
                failed += 1
            elif doc is not None:
                docs[path] = doc
    return docs, failed


def recipe_item_ids(api, namespace: str) -> list[str]:
    """Every recipe item Blizzard knows, by paging its item search in id order.

    The search returns at most 1,000 results per query, so each page starts
    after the last id of the one before.
    """
    found, after = [], 0
    for _ in range(40):
        doc, _ = api.search("/data/wow/search/item", {
            "item_class.id": RECIPE_CLASS, "orderby": "id", "_pageSize": 1000,
            "id": f"[{after + 1},]"}, namespace)
        ids = [r["data"]["id"] for r in doc.get("results") or [] if isinstance((r.get("data") or {}).get("id"), int)]
        if not ids:
            break
        found += [str(i) for i in ids]
        after = max(ids)
    return found


def taught_by_items(api, namespace: str, items: dict, cache: dict, limit: int) -> None:
    """Read recipe items not looked at yet: cache[item id] = [made item, profession, skill]."""
    known = [i for i, v in items.items() if v[2] == "Recipe"]
    try:
        known = sorted(set(known) | set(recipe_item_ids(api, namespace)), key=int)
    except (ApiError, NotFound, KeyError):
        pass                                        # no search: use the ones seen on the auction house
    todo = [i for i in known if i not in cache][:limit]
    docs, _ = fetch_all(api, [f"/data/wow/item/{i}" for i in todo], namespace)
    for path, doc in docs.items():
        preview = doc.get("preview_item") or {}
        made = (((preview.get("recipe") or {}).get("item") or {}).get("item") or {}).get("id")
        skill = (preview.get("requirements") or {}).get("skill") or {}
        level = skill.get("level")
        if isinstance(made, int) and isinstance(level, int):
            cache[path.rsplit("/", 1)[1]] = [made, text((skill.get("profession") or {}).get("name")), level]
        else:
            cache[path.rsplit("/", 1)[1]] = []


def build(api, region: str, items: dict, classic: dict, item_cache: dict, log=print) -> dict:
    namespace = f"static-forever-{region}"
    index, _ = api.get("/data/wow/profession/index", namespace)
    wanted: list[tuple[str, int]] = []
    for prof in index.get("professions", []):
        doc, _ = api.get(f"/data/wow/profession/{prof['id']}", namespace)
        for tier in doc.get("skill_tiers") or []:
            tier_doc, _ = api.get(f"/data/wow/profession/{prof['id']}/skill-tier/{tier['id']}", namespace)
            for category in tier_doc.get("categories") or []:
                for recipe in category.get("recipes") or []:
                    wanted.append((text(prof.get("name")), recipe["id"]))
    if not wanted:
        raise ApiError("the profession index lists no recipes")

    docs, failed = fetch_all(api, [f"/data/wow/recipe/{rid}" for _, rid in wanted], namespace)
    if failed > len(wanted) * 0.05:
        raise ApiError(f"{failed} of {len(wanted)} recipes could not be read")

    taught_by_items(api, namespace, items, item_cache, MAX_ITEM_LOOKUPS)
    taught = {}                                     # (profession, made item) -> skill
    for entry in item_cache.values():
        if entry:
            taught[(entry[1], entry[0])] = entry[2]

    names, quality, vendor, professions = {}, {}, {}, {}
    no_skill = 0
    for prof, rid in wanted:
        doc = docs.get(f"/data/wow/recipe/{rid}")
        made = (doc or {}).get("crafted_item") or {}
        if not isinstance(made.get("id"), int):
            continue                                # enchants and the like make no item
        flat = []
        for entry in doc.get("reagents") or []:
            mat = entry.get("reagent") or {}
            if not isinstance(mat.get("id"), int):
                continue
            flat += [mat["id"], entry.get("quantity") or 1]
            names[str(mat["id"])] = text(mat.get("name"))
            price = classic["vendor"].get(str(mat["id"]))
            if price:
                vendor[str(mat["id"])] = price
        if not flat:
            continue
        amount = doc.get("crafted_quantity") or {}
        low = amount.get("minimum", amount.get("value", 1)) or 1
        high = amount.get("maximum", amount.get("value", low)) or low
        low, high = (int(v) if float(v).is_integer() else v for v in (low, high))
        skill = taught.get((prof, made["id"]))
        if skill is None:
            skill = classic["skill"].get(prof, {}).get(str(made["id"]))
        no_skill += skill is None
        names[str(made["id"])] = text(made.get("name")) or text(doc.get("name"))
        known = items.get(str(made["id"]))
        quality[str(made["id"])] = known[1] if known else classic["quality"].get(str(made["id"]), 1)
        professions.setdefault(prof, []).append([made["id"], skill, low, high, flat])

    # Blizzard's index also carries a test profession and a few stubs.
    professions = {name: rows for name, rows in professions.items()
                   if len(rows) >= 5 and "[" not in name and "test" not in name.lower()}
    for rows in professions.values():
        rows.sort(key=lambda r: (r[1] is None, r[1] or 0, r[0]))
    total = sum(len(v) for v in professions.values())
    log(f"recipes: {total} from Blizzard across {len(professions)} professions, "
        f"{no_skill} without a skill level, {failed} unreadable")
    return {"official": True, "built": int(time.time()), "version": VERSION,
            "source": "Blizzard Forever profession data",
            "names": names, "quality": quality, "vendor": vendor,
            "professions": dict(sorted(professions.items()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="public")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--force", action="store_true", help="rebuild even if recent")
    args = parser.parse_args()
    data = Path(args.out) / "data"
    target = data / "recipes.json"
    old = read_json(target)
    if (old and not args.force and old.get("version") == VERSION
            and time.time() - old.get("built", 0) < REBUILD_AFTER):
        print("recipes: up to date")
        return 0
    try:
        cfg = read_json(Path(args.config)) or {}
        region = cfg.get("region", "eu")
        api = Api(region, os.environ.get("BLIZZARD_CLIENT_ID", "").strip(),
                  os.environ.get("BLIZZARD_CLIENT_SECRET", "").strip(), timeout=30)
        classic = read_json(Path(__file__).with_name("classic.json"))
        item_cache = read_json(data / "recipe_items.json") or {}
        out = build(api, region, read_json(data / "items.json") or {}, classic, item_cache)
        write_json(data / "recipe_items.json", item_cache)
        write_json(target, out)
    except Exception as err:                        # never block the price update
        print(f"::warning::Recipes were not rebuilt: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
