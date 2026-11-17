#!/usr/bin/env python3
"""One-off look at what Blizzard's Forever API returns for a few paths.

    BLIZZARD_CLIENT_ID=... BLIZZARD_CLIENT_SECRET=... python3 tools/probe.py PATH [PATH ...]

Prints each response, trimmed. Used to work out undocumented endpoints.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import collect  # noqa: E402


def trim(obj, depth=0):
    if isinstance(obj, dict):
        return {k: trim(v, depth + 1) for k, v in obj.items() if k not in ("_links", "key", "media")}
    if isinstance(obj, list):
        return [trim(v, depth + 1) for v in obj[:12]] + (["... %d more" % (len(obj) - 12)] if len(obj) > 12 else [])
    return obj


api = collect.Api("eu", os.environ["BLIZZARD_CLIENT_ID"].strip(), os.environ["BLIZZARD_CLIENT_SECRET"].strip())
for arg in sys.argv[1:]:
    path, _, namespace = arg.partition("@")
    namespace = namespace or "static-forever-eu"
    try:
        if "?" in path:                          # a search: the query is already started
            doc, _ = api._get_url(f"{api.host}{path}&namespace={namespace}&locale=en_GB", 2)
        else:
            doc, _ = api.get(path, namespace, tries=2)
        print(f"== {path} [{namespace}]\n{json.dumps(trim(doc), ensure_ascii=False)[:3500]}")
    except collect.NotFound:
        print(f"== {path} [{namespace}]\n404")
    except collect.ApiError as err:
        print(f"== {path} [{namespace}]\nERROR {err}")
