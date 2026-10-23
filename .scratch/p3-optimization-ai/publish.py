# 把 .scratch/p3-optimization-ai/issues/*.md 发布为 GitHub Issue，并建立阻塞边。
# 用法: python publish.py [--dry]
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ISSUES = os.path.join(HERE, "issues")

DRY = "--dry" in sys.argv


def run(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


parsed = []
for fn in sorted(os.listdir(ISSUES)):
    if not fn.endswith(".md"):
        continue
    num = int(fn.split("-")[0])
    path = os.path.join(ISSUES, fn)
    with io.open(path, "r", encoding="utf-8") as fh:
        body = fh.read()
    first = body.splitlines()[0]
    title = re.sub(r"^#\s*\d+:\s*", "", first).strip()
    blocked = re.findall(r"^- #(\d+)", body.split("## Blocked by")[-1], re.M)
    parsed.append({"num": num, "title": title, "body": body,
                   "blocked_by": sorted(set(int(x) for x in blocked))})

print("tickets to publish:")
for t in parsed:
    print("  #%d %s  (blocked by %r)" % (t["num"], t["title"], t["blocked_by"]))

if DRY:
    sys.exit(0)

# 发布（保持顺序，便于人工核对）
created = {}
for t in parsed:
    rc, out = run(["gh", "issue", "create", "--title", t["title"],
                   "--label", "ready-for-agent", "--body-file",
                   os.path.join(ISSUES, [f for f in os.listdir(ISSUES)
                                         if f.startswith("%d-" % t["num"])][0])])
    print("#%d create rc=%d %s" % (t["num"], rc, out.strip()[-120:]))
    if rc != 0:
        print("ABORT on #%d" % t["num"])
        sys.exit(1)
    m = re.search(r"/issues/(\d+)", out)
    created[t["num"]] = int(m.group(1))

print("created:", created)
with io.open(os.path.join(HERE, "publish-map.json"), "w", encoding="utf-8") as fh:
    import json
    json.dump(created, fh, indent=2)
print("map written")
