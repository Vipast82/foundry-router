"""Fixture project check: every task file listed in tasks.json that exists
must contain the word OK. Prints ALL PASS, or FAIL lines and SOME FAIL."""
import json
import os
import sys

bad = []
for t in json.load(open("tasks.json")):
    for f in t.get("scope", []):
        if os.path.isfile(f) and "OK" not in open(f).read():
            bad.append(f)
for b in bad:
    print("FAIL:", b, "does not contain OK")
print("ALL PASS" if not bad else "SOME FAIL")
sys.exit(1 if bad else 0)
