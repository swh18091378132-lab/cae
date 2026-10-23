# -*- coding: utf-8 -*-
"""
abaqus_runner.py -- Abaqus 求解与抽取的宿主侧封装（票 05）

对应 ADR-0006 / ADR-0012：
  * 求解：`abaqus job=<job> inp=<inp> interactive`（真机 Abaqus 2023）
  * 抽取：`abaqus python extract.py <odb> <out>`（在 Abaqus 内置 Python 2.7 内）
  * 宿主与 Abaqus **只交换文本**（.inp / .odb / CSV / JSON），不传对象

与 catia_driver 一样提供 **协议 + Fake + Real** 三件套，测试不依赖真机。

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


class SolveResult(object):
    def __init__(self, job_name, odb_path, status, log_tail=""):
        self.job_name = job_name
        self.odb_path = odb_path
        self.status = status
        self.log_tail = log_tail

    def __repr__(self):
        return "SolveResult(%s, %s)" % (self.job_name, self.status)


class ExtractResult(object):
    def __init__(self, csv_path, manifest_path, manifest):
        self.csv_path = csv_path
        self.manifest_path = manifest_path
        self.manifest = manifest


class AbaqusRunner(object):
    """协议：真机与 fake 都实现 solve() / extract()。"""

    def solve(self, inp_path, job_name, work_dir, step_name=None):
        raise NotImplementedError

    def extract(self, odb_path, out_dir, step_name=None):
        raise NotImplementedError


class FakeAbaqusRunner(AbaqusRunner):
    """
    测试用：不调用 Abaqus，直接产出合乎格式的 CSV + manifest。
    可人为制造极值（hotspot）以驱动票 06 校核。
    """

    def __init__(self, n_elems=6, n_plies=2, inject_hotspot=True):
        self.n_elems = n_elems
        self.n_plies = n_plies
        self.inject_hotspot = inject_hotspot

    def solve(self, inp_path, job_name, work_dir, step_name=None):
        if not os.path.isdir(work_dir):
            os.makedirs(work_dir)
        odb = os.path.join(work_dir, "%s.odb" % job_name)
        with open(odb, "w", encoding="utf-8") as fh:
            fh.write("FAKE ODB for %s\n" % job_name)
        return SolveResult(job_name, odb, "COMPLETED")

    def extract(self, odb_path, out_dir, step_name=None):
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        job = os.path.splitext(os.path.basename(odb_path))[0]
        csv_path = os.path.join(out_dir, "%s_elem_ply.csv" % job)
        rows = []
        # 每个单元每一层给一组应力；其中制造一个热点(高应力)供校核捕获
        for e in range(1, self.n_elems + 1):
            for sp in range(1, self.n_plies + 1):
                base = 10.0 + e * 2.0 + sp
                if self.inject_hotspot and e == self.n_elems and sp == self.n_plies:
                    base = 900.0   # 热点：接近许用值
                rows.append((e, sp, base))
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("field,elem,node,ip,sp,component,value\n")
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
            "step": step_name or "Static-1",
            "frame_value": 1.0,
            "n_rows": len(rows),
            "n_section_points": self.n_plies,
            "section_points": list(range(1, self.n_plies + 1)),
            "position": "CENTROID",
            "units_note": "fake units MPa",
            "csv": os.path.basename(csv_path),
        }
        man_path = os.path.join(out_dir, "%s_manifest.json" % job)
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
        return ExtractResult(csv_path, man_path, manifest)


class RealAbaqusRunner(AbaqusRunner):
    """
    真机：调用 Abaqus 2023（ADR-0012 实测版本）。

    abaqus_path: abaqus.bat 全路径；默认探测 D:\\abaqus2023 与 PATH。
    """

    def __init__(self, abaqus_path=None, timeout_sec=1800):
        self.abaqus_path = abaqus_path or self._detect()
        self.timeout_sec = timeout_sec

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

    def solve(self, inp_path, job_name, work_dir, step_name=None):
        if not os.path.isdir(work_dir):
            os.makedirs(work_dir)
        # 把 inp copy 到工作目录（Abaqus 在 cwd 下产出 .odb/.dat/.msg）
        local_inp = os.path.join(work_dir, os.path.basename(inp_path))
        if os.path.abspath(inp_path) != os.path.abspath(local_inp):
            shutil.copy2(inp_path, local_inp)

        rc, out = self._run(
            ["job=%s" % job_name,
             "inp=%s" % os.path.basename(local_inp),
             "interactive"], work_dir)

        odb = os.path.join(work_dir, "%s.odb" % job_name)
        status = "COMPLETED" if os.path.isfile(odb) and "COMPLETED" in out else "FAILED"
        tail = "\n".join(out.strip().splitlines()[-12:])
        return SolveResult(job_name, odb, status, tail)

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
            raise RuntimeError("未产出 CSV:\n%s" % out[-2000:])
        with open(man_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        return ExtractResult(csv_path, man_path, manifest)
