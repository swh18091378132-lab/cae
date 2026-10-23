# -*- coding: utf-8 -*-
"""
abaqus_runner.py -- Abaqus 求解与抽取的宿主侧封装（票 05）

对应 ADR-0006 / ADR-0012：
  * 求解：`abaqus job=<job> inp=<inp> interactive`（真机 Abaqus 2023）
  * 抽取：`abaqus python extract.py <odb> <out>`（在 Abaqus 内置 Python 2.7 内）
  * 宿主与 Abaqus **只交换文本**（.inp / .odb / CSV / JSON），不传对象

与 catia_driver 一样提供 **协议 + Fake + Real** 三件套，测试不依赖真机。

## 票14：分析类型

`solve()` 增加 `analysis` 参数（分析类型声明 dict 或 None），fake 与 real **同签名**。
分析类型在 Abaqus 侧的落实分两处，彼此不重叠：

  * **.inp 内容**：分析步 / 接触 / 损伤起始 / 元素类型（见 fe_build.py）。
    求解命令本身不变——Abaqus 从输入文件里的过程关键字自动判断 Standard / Explicit。
  * **ODB 元信息**：分析类型随 ODB 一起落盘（fake 写进 ODB，real 落在 manifest），
    抽取时原样记进 manifest，于是"这次结果是按哪种分析跑的"可追溯、可回归。

**抽取格式与分析类型无关**（票14 硬验收）：无论哪种类型，都是同一套
element × section_point × field 的 CSV + 同一份 manifest 口径，强度校核逻辑
不需要按分析类型分支。分析类型只出现在 manifest 的元信息字段里，永不进数据列。

兼容 Python 3.8（宿主侧）。
"""

import os
import sys
import json
import shutil
import subprocess

try:
    from .study import add_artifact, link
except ImportError:
    from study import add_artifact, link


# 抽取 CSV 的表头（fake 与 real 必须一致；ADR-0006 的中性文本口径）
CSV_HEADER = ["field", "elem", "node", "ip", "sp", "component", "value"]

# manifest 里与本票相关的元信息键（测试用来断言"口径一致"）
FIXED_MANIFEST_KEYS = (
    "schema", "job", "source_odb", "source_odb_sha256", "abaqus_version",
    "python_version", "step", "frame_value", "fields", "fields_present",
    "position", "integration_point_mode", "n_rows", "n_section_points",
    "section_points", "units_note", "csv", "analysis_kind",
)

# 未声明分析类型时按 P1 的静力默认（零回归）
_DEFAULT_ANALYSIS = {"kind": "static", "step_name": "Static-1"}

# fake ODB 里记录分析声明的标记行前缀（人类可读 + 可机器解析）
_ANALYSIS_MARK = "ANALYSIS-JSON: "


def _analysis_mod():
    """宿主侧的分析类型定义（唯一权威处在 core/analysis.py）。"""
    try:
        from . import analysis as analysis_mod
    except ImportError:
        import analysis as analysis_mod
    return analysis_mod


class SolveResult(object):
    def __init__(self, job_name, odb_path, status, log_tail="", analysis=None):
        self.job_name = job_name
        self.odb_path = odb_path
        self.status = status
        self.log_tail = log_tail
        # 票14：本次求解所用的分析类型声明（可追溯）
        self.analysis = analysis

    def __repr__(self):
        return "SolveResult(%s, %s)" % (self.job_name, self.status)


class ExtractResult(object):
    def __init__(self, csv_path, manifest_path, manifest,
                 rows=None, header=None):
        self.csv_path = csv_path
        self.manifest_path = manifest_path
        self.manifest = manifest
        # 解析后的 CSV 内容（表头 + 行），方便调用方直接断言口径
        self.header = list(header or CSV_HEADER)
        self.rows = list(rows or [])


def analysis_of(analysis):
    """
    把 None / kind 字符串 / dict 归一成分析声明 dict（只读，不改调用方对象）。

    归一与校验都委托 core/analysis.py —— 适配器不自带影子清单。
    """
    if analysis is None or analysis == "":
        return dict(_DEFAULT_ANALYSIS)
    mod = _analysis_mod()
    spec = mod.normalize(analysis)
    if spec is None:
        return dict(_DEFAULT_ANALYSIS)
    out = dict(spec)
    out.setdefault("step_name", mod.step_name(out))
    return out


class AbaqusRunner(object):
    """协议：真机与 fake 都实现 solve() / extract()。

    票14 起 solve() 增加 `analysis` 参数（分析类型声明 dict 或 None）。
    **fake 与 real 必须保持同签名**（票14 验收项），宿主侧只传声明，不传代码对象。
    """

    def solve(self, inp_path, job_name, work_dir, step_name=None, analysis=None):
        raise NotImplementedError

    def extract(self, odb_path, out_dir, step_name=None):
        raise NotImplementedError


class FakeAbaqusRunner(AbaqusRunner):
    """
    测试用：不调用 Abaqus，直接产出合乎格式的 CSV + manifest。
    可人为制造极值（hotspot）以驱动票 06 校核。

    票14 起支持全部分析类型，且**每种类型的抽取格式完全一致**（同字段同口径），
    使后续强度校核无需按类型分支；对同一 (n_elems, n_plies, inject_hotspot)
    输出必须**确定性可回归**——分析类型只改 manifest 元信息，不改数值口径。

    分析声明写进 fake ODB（求解产物）而不是留在实例属性上：这样"抽取的是哪次求解
    的产物"就由产物自己决定，多次求解 / 交叉抽取也不会张冠李戴。
    """

    def __init__(self, n_elems=6, n_plies=2, inject_hotspot=True):
        self.n_elems = n_elems
        self.n_plies = n_plies
        self.inject_hotspot = inject_hotspot
        # 记录最近一次求解用的分析类型，供测试断言"声明确实传到了适配器"
        self.last_analysis = None

    def solve(self, inp_path, job_name, work_dir, step_name=None, analysis=None):
        if not os.path.isdir(work_dir):
            os.makedirs(work_dir)
        spec = analysis_of(analysis)
        self.last_analysis = spec
        odb = os.path.join(work_dir, "%s.odb" % job_name)
        payload = {
            "job": job_name,
            "source_inp": os.path.abspath(inp_path) if inp_path else "",
            "step": step_name or spec["step_name"],
            "analysis": spec,
        }
        with open(odb, "w", encoding="utf-8") as fh:
            fh.write("FAKE ODB for %s\n" % job_name)
            fh.write("analysis=%s\n" % spec["kind"])
            # 声明原样落盘：求解口径可追溯（真机对应 .com/.msg）
            fh.write(_ANALYSIS_MARK + json.dumps(
                payload, ensure_ascii=False, sort_keys=True) + "\n")
        return SolveResult(job_name, odb, "COMPLETED", analysis=spec)

    # -- 内部 --------------------------------------------------------------

    def _read_analysis(self, odb_path):
        """
        从 fake ODB 读回分析声明。

        读不到（例如 ODB 不是本 fake 产出的）时退回最近一次求解的声明，
        再退回静力默认——只为兼容老产物，不改变抽取格式。
        """
        try:
            with open(odb_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(_ANALYSIS_MARK):
                        payload = json.loads(line[len(_ANALYSIS_MARK):])
                        spec = payload.get("analysis") or {}
                        return analysis_of(spec), payload.get("step")
        except (IOError, OSError, ValueError):
            pass
        spec = analysis_of(self.last_analysis)
        return spec, spec["step_name"]

    def extract(self, odb_path, out_dir, step_name=None):
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        job = os.path.splitext(os.path.basename(odb_path))[0]
        csv_path = os.path.join(out_dir, "%s_elem_ply.csv" % job)

        spec, recorded_step = self._read_analysis(odb_path)

        rows = []
        # 每个单元每一层给一组应力；其中制造一个热点(高应力)供校核捕获
        for e in range(1, self.n_elems + 1):
            for sp in range(1, self.n_plies + 1):
                base = 10.0 + e * 2.0 + sp
                if self.inject_hotspot and e == self.n_elems and sp == self.n_plies:
                    base = 900.0   # 热点：接近许用值
                rows.append((e, sp, base))
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(",".join(CSV_HEADER) + "\n")
            for (e, sp, s11) in rows:
                fh.write("S,%d,,1,%d,c1,%.6f\n" % (e, sp, s11))
                fh.write("S,%d,,1,%d,c2,%.6f\n" % (e, sp, s11 * 0.2))
                fh.write("U,%d,,,,'mag',%.6f\n" % (e, 0.001 * e))
        manifest = {
            "schema": "cae-extract-manifest/1",
            "job": job,
            "source_odb": odb_path,
            "source_odb_sha256": "fake",
            "abaqus_version": "FAKE",
            "python_version": "2.7.15-fake",
            "step": step_name or recorded_step or spec["step_name"],
            "frame_value": 1.0,
            "fields": ["S", "U", "E"],
            "fields_present": ["S", "U"],
            "integration_point_mode": "INTEGRATION_POINT",
            "n_rows": len(rows),
            "n_section_points": self.n_plies,
            "section_points": list(range(1, self.n_plies + 1)),
            "position": "CENTROID",
            "units_note": "fake units MPa",
            "csv": os.path.basename(csv_path),
            # 票14：分析类型只作为元信息记录，**不改抽取口径**
            "analysis_kind": spec["kind"],
            "analysis_step": spec["step_name"],
            "analysis": spec,
        }
        man_path = os.path.join(out_dir, "%s_manifest.json" % job)
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
        return ExtractResult(csv_path, man_path, manifest,
                             rows=rows, header=CSV_HEADER)


class RealAbaqusRunner(AbaqusRunner):
    """
    真机：调用 Abaqus 2023（ADR-0012 实测版本）。

    abaqus_path: abaqus.bat 全路径；默认探测 D:\\abaqus2023 与 PATH。
    """

    def __init__(self, abaqus_path=None, timeout_sec=1800):
        self.abaqus_path = abaqus_path or self._detect()
        self.timeout_sec = timeout_sec
        self.last_analysis = None

    @staticmethod
    def _detect():
        for p in (r"D:\abaqus2023\Commands\abaqus.bat",
                  r"C:\SIMULIA\Commands\abaqus.bat"):
            if os.path.isfile(p):
                return p
        found = shutil.which("abaqus.bat") or shutil.which("abaqus")
        if found:
            return found
        raise RuntimeError("未找到 abaqus 命令，请显式传入 abaqus_path")

    def _run(self, args, cwd):
        cmd = [self.abaqus_path] + list(args)
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, shell=False)
        try:
            out, _ = proc.communicate(timeout=self.timeout_sec)
        except Exception:
            proc.kill()
            out, _ = proc.communicate()
        return proc.returncode, out

    def solve(self, inp_path, job_name, work_dir, step_name=None, analysis=None):
        if not os.path.isdir(work_dir):
            os.makedirs(work_dir)
        # 把 inp copy 到工作目录（Abaqus 在 cwd 下产出 .odb/.dat/.msg）
        local_inp = os.path.join(work_dir, os.path.basename(inp_path))
        if os.path.abspath(inp_path) != os.path.abspath(local_inp):
            shutil.copy2(inp_path, local_inp)

        # 分析类型已在 .inp 的分析步里体现（fe_build._step_block），
        # 故求解命令本身不变；这里记录声明以便追溯。
        spec = analysis_of(analysis)
        self.last_analysis = spec

        rc, out = self._run(
            ["job=%s" % job_name,
             "inp=%s" % os.path.basename(local_inp),
             "interactive"], work_dir)

        odb = os.path.join(work_dir, "%s.odb" % job_name)
        status = "COMPLETED" if os.path.isfile(odb) and "COMPLETED" in out else "FAILED"
        tail = "\n".join(out.strip().splitlines()[-12:])
        return SolveResult(job_name, odb, status, tail, analysis=spec)

    def extract(self, odb_path, out_dir, step_name=None):
        # 本文件在 <repo>/apps/workbench/core/，extract.py 在 <repo>/abaqus_capabilities/
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        script = os.path.join(repo_root, "abaqus_capabilities", "extract.py")
        if not os.path.isfile(script):
            raise RuntimeError("extract.py 未找到: %s" % script)

        args = ["python", script, os.path.abspath(odb_path), os.path.abspath(out_dir)]
        if step_name:
            args.append(step_name)
        rc, out = self._run(args, os.path.dirname(os.path.abspath(odb_path)))
        if rc != 0:
            raise RuntimeError("抽取失败(rc=%d):\n%s" % (rc, out[-2000:]))

        job = os.path.splitext(os.path.basename(odb_path))[0]
        csv_path = os.path.join(out_dir, "%s_elem_ply.csv" % job)
        man_path = os.path.join(out_dir, "%s_manifest.json" % job)
        if not os.path.isfile(csv_path):
            raise RuntimeError("未产出 CSV:\n%s" % (out[-2000:]))
        with open(man_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        # 真机 extract.py 在 Abaqus 内置 Python 2.7 跑，不知道分析类型；
        # 由宿主侧把它补进 manifest（抽取口径本身不变，只补元信息）。
        spec = analysis_of(self.last_analysis)
        manifest.setdefault("analysis_kind", spec["kind"])
        manifest.setdefault("analysis_step", spec["step_name"])
        manifest.setdefault("analysis", spec)
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)

        header, rows = _read_csv(csv_path)
        return ExtractResult(csv_path, man_path, manifest,
                             rows=rows, header=header)


def _read_csv(csv_path):
    """读回抽取 CSV（表头 + 行），供调用方断言"口径一致"。"""
    import csv as _csv
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        rd = _csv.reader(fh)
        try:
            header = next(rd)
        except StopIteration:
            return [], []
        rows = [r for r in rd if r]
    return header, rows
