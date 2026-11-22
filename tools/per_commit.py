"""Per-commit breakdown of work/out.jsonl against the sample answers."""
import json
tests = {t["number"]: t for t in json.load(open("sample/tests.json"))["tests"]}
ans = json.load(open("sample/answers.json"))["commits"]
why = {l.split(" ", 1)[0]: l.strip() for l in open("work/why.log") if l.strip()}
tot = 0
for line in open("work/out.jsonl"):
    o = json.loads(line)
    sel = {n for w in o["workers"] for n in w}
    loads = [sum(round(tests[n]["seconds"] * 1000) for n in w) for w in o["workers"] if w]
    cost = (max(loads) + 2000 * len(loads)) if loads else 0
    a = ans[o["commit"]]
    miss = [n for n in a["failing"] if n not in sel]
    cost += 300000 * bool(miss)
    tot += cost
    print(f'{o["commit"]} {a["kind"]:8} sel={len(sel):5} k={len(loads)} cost={cost/1000:7.1f} {"MISS "+str([tests[n]["id"] for n in miss]) if miss else ""}  | {why.get(o["commit"], "")[5:160]}')
print("total", tot / 1000)
