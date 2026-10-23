# -*- coding: utf-8 -*-
"""
extract.py -- ODB 结果抽取器（票 05）

**在 Abaqus 内置 Python 2.7.15 中运行**（ADR-0012 实测）。
职责：按口径从 ODB 抽出 element x ply(sectionPoint) x 场量，写成中性文本
（CSV + JSON manifest），供宿主侧（Python 3.8）入库与校核。

设计约束（ADR-0006）：
  * 只吐 CSV/JSON + manifest，不传对象
  * manifest 记录：来源 ODB 指纹、Abaqus 版本、抽取口径(section point / 积分点 /
    单位)、元素与铺层数量、抽取时间，保证可复现
  * 抽取口径写死，不靠隐式默认

用法（在 Abaqus 内）:
    abaqus python extract.py <odb路径> <输出目录> [step_name]

输出:
    <out>/<job>_elem_ply.csv     每行 = 单元 x 铺层(或单层) 的场量
    <out>/<job>_manifest.json    元数据与口径
"""

import os
import sys
import csv
import json
import hashlib
import datetime


# ---- 口径常量（写死，manifest 里回报，保证可复现） ----
INTEGRATION_POINT_MODE = "INTEGRATION_POINT"
POSITION_NAME = "CENTROID"     # 取单元形心处，与单元级校核口径一致
FIELDS = ["S", "U", "E"]


def _sha256_file(path):
    h = hashlib.sha256()
    f = open(path, "rb")
    try:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    finally:
        f.close()
    return h.hexdigest()


def _abaqus_version():
    """Abaqus 版本：优先 ODB 自带，退回 environment/info。"""
    try:
        import odbAccess
        # 2.7 下 abaqus 模块无 __version__；尝试若干来源
        for modname in ("abaqus", "abaqusConstants", "odbAccess"):
            try:
                mod = __import__(modname)
                for attr in ("__version__", "version", "Version"):
                    v = getattr(mod, attr, None)
                    if v:
                        return str(v)
            except Exception:
                pass
    except Exception:
        pass
    # 最后退回环境变量（由宿主传入）
    try:
        return os.environ.get("ABAQUS_VERSION", "unknown")
    except Exception:
        return "unknown"


def _python_version():
    return "%d.%d.%d" % (sys.version_info[0], sys.version_info[1],
                         sys.version_info[2])


def _name(obj):
    """S/OdbObject 的 name 属性（2.7 下可能是 unicode）。"""
    try:
        return str(obj.name)
    except Exception:
        return str(obj)


def extract(odb_path, out_dir, step_name=None, fields=None, max_values=None):
    """
    从 ODB 抽取结果。返回 manifest dict（同时写盘）。

    参数:
        odb_path  : .odb 路径
        out_dir   : 输出目录
        step_name : 指定 step；None 取最后一个 step
        fields    : 覆盖默认场量列表
        max_values: 调试用，限制每个场量抽取的 value 数（None=全量）
    """
    from odbAccess import openOdb

    fields = fields or FIELDS
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    odb_sha = _sha256_file(odb_path)
    odb = openOdb(odb_path, readOnly=True)
    try:
        # ---- step / frame ----
        step_keys = list(odb.steps.keys())
        if step_name is None:
            step_name = step_keys[-1]
        step = odb.steps[step_name]
        frame = step.frames[-1]

        # ---- 抽取 S / E（按 element + sectionPoint），U（按 node） ----
        rows = []
        n_section_points = 0
        seen_sp = {}

        for fname in fields:
            if fname not in frame.fieldOutputs.keys():
                continue
            fo = frame.fieldOutputs[fname]

            if fname == "U":
                # 位移是节点量
                values = fo.values
                total = len(values)
                if max_values:
                    values = values[:max_values]
                for v in values:
                    rows.append({
                        "field": "U",
                        "elem": "",
                        "node": v.nodeLabel,
                        "ip": "",
                        "sp": "",
                        "component": "mag",
                        "value": "%.9g" % float(
                            (v.data[0] ** 2 + v.data[1] ** 2 + v.data[2] ** 2) ** 0.5),
                    })
                continue

            # S / E : 单元量，含 sectionPoint（铺层）
            try:
                subset = fo.getSubset(position=POSITION_NAME)
            except Exception:
                subset = fo
            values = subset.values
            total = len(values)
            if max_values:
                values = values[:max_values]

            for v in values:
                sp = ""
                if getattr(v, "sectionPoint", None) is not None:
                    try:
                        sp = v.sectionPoint.number
                        seen_sp[sp] = True
                    except Exception:
                        sp = ""
                elif getattr(v, "sectionPoint", None) is not None:
                    sp = ""

                elem = getattr(v, "elementLabel", "")
                ip = getattr(v, "integrationPoint", "")

                # 主应力/主分量 + von Mises（S 才有）
                comps = []
                data = v.data
                for k in range(len(data)):
                    comps.append(("c%d" % (k + 1), float(data[k])))
                if fname == "S":
                    try:
                        comps.append(("mises", float(v.mises)))
                    except Exception:
                        pass
                    try:
                        comps.append(("maxPrincipal", float(v.maxPrincipal)))
                        comps.append(("minPrincipal", float(v.minPrincipal)))
                    except Exception:
                        pass
                else:
                    try:
                        comps.append(("mises", float(v.mises)))
                    except Exception:
                        pass

                for cname, cval in comps:
                    rows.append({
                        "field": fname,
                        "elem": elem,
                        "node": "",
                        "ip": ip,
                        "sp": sp,
                        "component": cname,
                        "value": "%.9g" % cval,
                    })

        n_section_points = len(seen_sp)
        job_name = os.path.splitext(os.path.basename(odb_path))[0]

        # ---- 写 CSV ----
        csv_path = os.path.join(out_dir, "%s_elem_ply.csv" % job_name)
        fh = open(csv_path, "wb")
        try:
            w = csv.writer(fh)
            w.writerow(["field", "elem", "node", "ip", "sp", "component", "value"])
            for r in rows:
                w.writerow([r["field"], r["elem"], r["node"], r["ip"],
                            r["sp"], r["component"], r["value"]])
        finally:
            fh.close()

        # ---- manifest ----
        manifest = {
            "schema": "cae-extract-manifest/1",
            "job": job_name,
            "source_odb": os.path.abspath(odb_path),
            "source_odb_sha256": odb_sha,
            "abaqus_version": _abaqus_version(),
            "python_version": _python_version(),
            "step": step_name,
            "step_keys": step_keys,
            "frame_value": float(frame.frameValue),
            "fields": fields,
            "fields_present": list(frame.fieldOutputs.keys()),
            "position": POSITION_NAME,
            "integration_point_mode": INTEGRATION_POINT_MODE,
            "n_rows": len(rows),
            "n_section_points": n_section_points,
            "section_points": sorted(seen_sp.keys()),
            "units_note": "Abaqus 模型单位制由建模端决定，P1 使用 mm-N-MPa",
            "extracted_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "csv": os.path.basename(csv_path),
        }
        man_path = os.path.join(out_dir, "%s_manifest.json" % job_name)
        fh = open(man_path, "wb")
        try:
            fh.write(json.dumps(manifest, indent=2, sort_keys=True))
        finally:
            fh.close()

        return manifest
    finally:
        odb.close()


def main(argv):
    if len(argv) < 3:
        print("usage: abaqus python extract.py <odb> <out_dir> [step]")
        return 2
    odb_path = argv[1]
    out_dir = argv[2]
    step = argv[3] if len(argv) > 3 else None
    m = extract(odb_path, out_dir, step)
    print("extracted %d rows, %d section points -> %s"
          % (m["n_rows"], m["n_section_points"], out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
