#!/usr/bin/env python3
"""Build the Classic fallback data from the open wow-classic-items dataset (MIT).

Writes site/recipes.json, the recipe list the dashboard falls back to when
Blizzard's Forever recipes (collector/recipes.py) have not been published, and
collector/classic.json, the skill levels and vendor prices that script borrows.

    npm pack wow-classic-items && tar xzf wow-classic-items-*.tgz
    python3 tools/build_recipes.py package/data/json/data.json

The dataset describes recipes as they stood in Wrath Classic, so a few old
recipes (flasks, for one) carry later material counts. Keeps the
original-Classic professions only: skill 300 or lower, and no
Jewelcrafting or Inscription, which Forever does not have. Recipes whose
materials do not exist in Forever are dropped by the dashboard at run time,
because they never get a price.

Output shape:
    names        item id -> name, for every product and material
    quality      product id -> 0..5
    vendor       item id -> price when a vendor sells it (copper)
    professions  name -> [[product, skill, yieldMin, yieldMax, [mat, n, ...]], ...]
"""
import json
import sys
from pathlib import Path

SKIP = {"Jewelcrafting", "Inscription"}
LOTUS, VIAL = 13468, 8925
FLASKS = {                                  # product: [material, amount, ...]
    13506: [13423, 30, 13465, 10, LOTUS, 1, VIAL, 1],   # Petrification: Stonescale Oil, Mountain Silversage
    13510: [8846, 30, 13423, 10, LOTUS, 1, VIAL, 1],    # the Titans: Gromsblood, Stonescale Oil
    13511: [13463, 30, 13467, 10, LOTUS, 1, VIAL, 1],   # Distilled Wisdom: Dreamfoil, Icecap
    13512: [13463, 30, 13465, 10, LOTUS, 1, VIAL, 1],   # Supreme Power: Dreamfoil, Mountain Silversage
    13513: [13467, 30, 13465, 10, LOTUS, 1, VIAL, 1],   # Chromatic Resistance: Icecap, Mountain Silversage
}
QUALITY = {"Poor": 0, "Common": 1, "Uncommon": 2, "Rare": 3, "Epic": 4, "Legendary": 5}


def main(src: str) -> None:
    items = json.loads(Path(src).read_text(encoding="utf-8"))
    by_id = {i["itemId"]: i for i in items}
    names, quality, vendor, professions = {}, {}, {}, {}
    for item in items:
        for made in item.get("createdBy") or []:
            if made["category"] in SKIP or made["requiredSkill"] > 300:
                continue
            flat = []
            for mat in made["reagents"]:
                flat += [mat["itemId"], mat["amount"]]
                ref = by_id[mat["itemId"]]
                names[mat["itemId"]] = ref["name"]
                if ref.get("vendorPrice"):
                    vendor[mat["itemId"]] = ref["vendorPrice"]
            names[item["itemId"]] = item["name"]
            quality[item["itemId"]] = QUALITY.get(item.get("quality"), 1)
            low, high = made["amount"]
            professions.setdefault(made["category"], []).append(
                [item["itemId"], made["requiredSkill"], low, high, flat])
    # The five old-world flasks were made cheaper in later expansions. Put the
    # original amounts back: 30 + 10 herbs, a Black Lotus and a Crystal Vial
    # for one flask.
    for row in professions.get("Alchemy", []):
        if row[0] in FLASKS:
            assert "Flask" in names[row[0]] or "Petrification" in names[row[0]], names[row[0]]
            row[2], row[3], row[4] = 1, 1, FLASKS[row[0]]
            for mat in row[4][0::2]:
                names[mat] = by_id[mat]["name"]
                if by_id[mat].get("vendorPrice"):
                    vendor[mat] = by_id[mat]["vendorPrice"]
    for rows in professions.values():
        rows.sort(key=lambda r: (r[1], r[0]))
    out = {"source": "wow-classic-items 2.0.1 (MIT), Wrath Classic recipe data",
           "names": names, "quality": quality, "vendor": vendor,
           "professions": dict(sorted(professions.items()))}
    # What the collector needs from Classic when it builds recipes from
    # Blizzard's Forever data: required skill by profession and item, which
    # materials a vendor sells and for how much, and item quality.
    classic = {"skill": {prof: {str(r[0]): r[1] for r in rows} for prof, rows in professions.items()},
               "vendor": {str(i["itemId"]): i["vendorPrice"] for i in items if i.get("vendorPrice")},
               "quality": {str(i["itemId"]): QUALITY.get(i.get("quality"), 1) for i in items if i.get("createdBy")}}
    side = Path(__file__).resolve().parents[1] / "collector" / "classic.json"
    side.write_text(json.dumps(classic, separators=(",", ":")), encoding="utf-8")
    target = Path(__file__).resolve().parents[1] / "site" / "recipes.json"
    target.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print({k: len(v) for k, v in out["professions"].items()}, target.stat().st_size, "bytes")


if __name__ == "__main__":
    main(sys.argv[1])
