"""Dev only: score selector output on the labeled part of a synthetic set.
    python3 tools/synth_eval.py work/synth [-v]"""
import json, sys
sys.path.insert(0, ".")
from selector import split
d = sys.argv[1]
tests = {t["number"]: t for t in json.load(open("sample/tests.json"))["tests"]}
ans = json.load(open(f"{d}/answers.json"))["commits"]
why = {l.split(" ", 1)[0]: l.strip() for l in open(f"{d}/why.log") if l.strip()}
tot = misses = n = 0
rows = []
allcost = []
for line in open(f"{d}/out.jsonl"):
    o = json.loads(line)
    _l = [sum(round(tests[x]["seconds"] * 1000) for x in w) for w in o["workers"] if w]
    allcost.append(((max(_l) + 2000 * len(_l)) / 1000 if _l else 0, o["commit"]))
    if o["commit"] not in ans:
        continue
    sel = {x for w in o["workers"] for x in w}
    loads = [sum(round(tests[x]["seconds"] * 1000) for x in w) for w in o["workers"] if w]
    cost = (max(loads) + 2000 * len(loads)) / 1000 if loads else 0
    a = ans[o["commit"]]
    miss = [x for x in a["failing"] if x not in sel]
    oracle = [x for x in a["failing"]]
    ol = sum(round(tests[x]["seconds"] * 1000) for x in oracle) / 1000
    if oracle:
        ms = {x: round(tests[x]["seconds"] * 1000) for x in oracle}
        ws = split(set(oracle), ms)
        ocost = (max(sum(ms[x] for x in w) for w in ws) + 2000 * len(ws)) / 1000
    else:
        ocost = 0
    globals().setdefault("otot", 0)
    globals()["otot"] += ocost
    n += 1; tot += cost; misses += bool(miss)
    rows.append((cost + 300 * bool(miss), o["commit"], a["kind"], len(sel), len(a["failing"]),
                 [tests[x]["id"].split("::")[-1] for x in miss][:3], why.get(o["commit"], "")[5:140]))
rows.sort(reverse=True)
if "-v" in sys.argv:
    for r in rows:
        print(f"{r[0]:6.1f} {r[1]} {r[2]:8} sel={r[3]:4} fail={r[4]:3} {'MISS'+str(r[5]) if r[5] else ''} | {r[6]}")
if "-a" in sys.argv:
    for c, k in sorted(allcost, reverse=True)[:25]:
        print(f"{c:6.1f} {k} {'L' if k in ans else ' '} | {why.get(k, '')[5:150]}")
print(f"all={len(allcost)} cost={sum(c for c, _ in allcost):.1f} per30={sum(c for c, _ in allcost)*30/len(allcost):.1f}")
print(f"oracle={globals().get('otot', 0):.1f} per30={globals().get('otot', 0)*30/max(n,1):.1f}")
print(f"labeled={n} cost={tot:.1f} misses={misses} total={tot+300*misses:.1f} per30={(tot+300*misses)*30/max(n,1):.1f}")
