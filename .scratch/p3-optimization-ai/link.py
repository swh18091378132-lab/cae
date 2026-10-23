# 建立原生阻塞边（issue dependencies）+ 子 issue 关联，并回填本地文件的发布说明
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "swh18091378132-lab/cae"
PARENT = 20

# 票 -> 阻塞它的票
EDGES = {
    23: [21],
    24: [23],
    25: [21, 22, 23],
    27: [24, 25],
    28: [25, 26],
}


def run(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


def db_id(n):
    rc, out = run(["gh", "api", "repos/%s/issues/%d" % (REPO, n)])
    if rc != 0:
        raise SystemExit("cannot read issue %d: %s" % (n, out[-300:]))
    import json
    return json.loads(out)["id"]


print("== native blocking edges ==")
for child, parents in sorted(EDGES.items()):
    for parent in parents:
        pid = db_id(parent)
        rc, out = run([
            "gh", "api", "--method", "POST",
            "repos/%s/issues/%d/dependencies/blocked_by" % (REPO, child),
            "-F", "issue_id=%d" % pid])
        print("  #%d blocked_by #%d -> rc=%d %s"
              % (child, parent, rc, out.strip()[:100]))

print("== parent sub-issues ==")
for n in range(21, 29):
    rc, out = run(["gh", "issue", "edit", str(PARENT),
                   "--add-sub-issue", str(n)])
    print("  parent #%d += #%d -> rc=%d %s" % (PARENT, n, rc, out.strip()[:100]))

print("== backfill local provenance ==")
ISSUES = os.path.join(HERE, "issues")
for fn in sorted(os.listdir(ISSUES)):
    if not fn.endswith(".md"):
        continue
    num = int(fn.split("-")[0])
    path = os.path.join(ISSUES, fn)
    with io.open(path, "r", encoding="utf-8") as fh:
        body = fh.read()
    marker = "> **已发布到 GitHub Issue：**"
    if marker in body:
        continue
    head = body.splitlines()[0]
    rest = "\n".join(body.splitlines()[1:])
    note = ("> **已发布到 GitHub Issue：** %s#%d（本文件为本地原文，"
            "工单状态以 GitHub 为准）\n" % (REPO, num))
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(head + "\n\n" + note + rest)
    print("  %s -> #%d" % (fn, num))

print("done")
