#!/usr/bin/env python3
"""Build site/recipes.json from the open wow-classic-items dataset (MIT).

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
    for rows in professions.values():
        rows.sort(key=lambda r: (r[1], r[0]))
    out = {"source": "wow-classic-items 2.0.1 (MIT), Wrath Classic recipe data",
           "names": names, "quality": quality, "vendor": vendor,
           "professions": dict(sorted(professions.items()))}
    target = Path(__file__).resolve().parents[1] / "site" / "recipes.json"
    target.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print({k: len(v) for k, v in out["professions"].items()}, target.stat().st_size, "bytes")


if __name__ == "__main__":
    main(sys.argv[1])
