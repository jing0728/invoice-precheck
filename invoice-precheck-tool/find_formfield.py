# -*- coding: utf-8 -*-
"""Locate where the custom-form id lives in the SO record XML — read only."""
import sys
from netsuite_scraper import PlaywrightSource, _clean

so_number = sys.argv[1] if len(sys.argv) > 1 else "SO9000007"
src = PlaywrightSource()
tid, root = src.resolve_so(so_number)
rec = root.find("record")
print("SO", so_number, "internal id", tid)
hits = []
for el in rec.iter():
    if el is rec:
        continue
    txt = (el.text or "").strip()
    if txt == "219" or "form" in el.tag.lower():
        hits.append((el.tag, txt[:60]))
for tag, txt in hits[:40]:
    print(f"  <{tag}> = {txt!r}")
src.close()
