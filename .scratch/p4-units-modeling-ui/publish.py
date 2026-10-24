# -*- coding: utf-8 -*-
"""发布 P4 票据到 GitHub，建立原生阻塞边与子 issue 关联，并回填本地出处。"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ISSUES = os.path.join(HERE, "issues")
REPO = "swh18091378132-lab/cae"
PARENT = 30
DRY = "--dry" in sys.argv
LINK_ONLY = "--link-only" in sys.argv


def run(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


parsed = []
for fn in sorted(os.listdir(ISSUES)):
    if not fn.endswith(".md"):
        continue
    num = int(fn.split(".")[0].split("-")[0])
    path = os.path.join(ISSUES, fn)
    with io.open(path, "r", encoding="utf-8") as fh:
        body = fh.read()
    first = body.splitlines()[0]
    title = re.sub(r"^#\s*\d+:\s*", "", first).strip()
    tail = body.split("## Blocked by")[-1]
    blocked = [int(x) for x in re.findall(r"^- #(\d+)", tail, re.M)]
    parsed.append({"num": num, "title": title, "path": path,
                   "body": body, "blocked_by": sorted(blocked)})

parsed.sort(key=lambda t: t["num"])
print("tickets:", len(parsed))
for t in parsed:
    print("  #%d %s  (blocked by %r)" % (t["num"], t["title"], t["blocked_by"]))

MAP = os.path.join(HERE, "publish-map.json")
created = {}
if os.path.exists(MAP):
    with io.open(MAP, "r", encoding="utf-8") as fh:
        created = dict((int(k), v) for k, v in json.load(fh).items())
    print("existing map:", created)

if DRY:
    sys.exit(0)

if not LINK_ONLY:
    for t in parsed:
        if t["num"] in created:
            print("#%d already published as #%d" % (t["num"], created[t["num"]]))
            continue
        rc, out = run(["gh", "issue", "create", "--title", t["title"],
                       "--label", "ready-for-agent",
                       "--body-file", t["path"]])
        print("#%d create rc=%d %s" % (t["num"], rc, out.strip()[-110:]))
        if rc != 0:
            sys.exit(1)
        m = re.search(r"/issues/(\d+)", out)
        created[t["num"]] = int(m.group(1))
    with io.open(MAP, "w", encoding="utf-8") as fh:
        json.dump(dict((str(k), v) for k, v in created.items()), fh, indent=2)
    print("map:", created)


def db_id(n):
    rc, out = run(["gh", "api", "repos/%s/issues/%d" % (REPO, n)])
    if rc != 0:
        raise SystemExit("cannot read issue %d: %s" % (n, out[-300:]))
    return json.loads(out)["id"]


print("== blocking edges ==")
for t in parsed:
    child = created[t["num"]]
    for parent in t["blocked_by"]:
        parent_issue = created.get(parent, parent)
        pid = db_id(parent_issue)
        rc, out = run(["gh", "api", "--method", "POST",
                       "repos/%s/issues/%d/dependencies/blocked_by"
                       % (REPO, child), "-F", "issue_id=%d" % pid])
        print("  #%d blocked_by #%d -> rc=%d %s"
              % (child, parent_issue, rc, out.strip()[:70]))

print("== sub-issues of #%d ==" % PARENT)
for t in parsed:
    rc, out = run(["gh", "issue", "edit", str(PARENT),
                   "--add-sub-issue", str(created[t["num"]])])
    print("  #%d -> rc=%d %s" % (created[t["num"]], rc, out.strip()[:70]))

print("== backfill provenance ==")
for t in parsed:
    num = created[t["num"]]
    with io.open(t["path"], "r", encoding="utf-8") as fh:
        body = fh.read()
    if "> **已发布到 GitHub Issue：**" in body:
        continue
    head, rest = body.splitlines()[0], "\n".join(body.splitlines()[1:])
    note = ("> **已发布到 GitHub Issue：** %s#%d（本文件为本地原文，"
            "工单状态以 GitHub 为准）\n" % (REPO, num))
    with io.open(t["path"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(head + "\n\n" + note + rest)
    print("  %s -> #%d" % (os.path.basename(t["path"]), num))
print("done")
