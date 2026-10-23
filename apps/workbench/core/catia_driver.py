# -*- coding: utf-8 -*-
"""
catia_driver.py -- CATIA 参数化驱动（M1）

* `CatiaDriver` 是协议（抽象基类）。测试注入 `FakeCatiaDriver`，真机用 `RealCatiaDriver`。
* 遵循 ADR-0004：模板 CATPart + 参数契约 + 参数化包络；外部 Automation 改参 + Part.Update()。
* RealCatiaDriver 依赖 win32com（pywin32），仅在真机调用时才 import，避免测试机缺库。

兼容 Python 3.8。
"""

import os
import hashlib
import datetime


class CatPartResult(object):
    def __init__(self, path, checksum, params_used):
        self.path = path
        self.checksum = checksum
        self.params_used = params_used

    def __repr__(self):
        return "CatPartResult(path=%s, sha=%s)" % (self.path, self.checksum[:12])


class CatiaDriver(object):
    """协议：真机与 fake 都实现 regenerate()。"""

    def regenerate(self, parameter_set, template):
        raise NotImplementedError


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class FakeCatiaDriver(CatiaDriver):
    """
    测试用：不碰 CATIA，只把参数写进一个桩 CATPart 文件，返回路径+指纹。
    可选 `fail_on` 参数名，用于模拟 CATIA 再生失败。
    """

    def __init__(self, out_dir, fail_on=None):
        self.out_dir = out_dir
        self.fail_on = fail_on or []
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)

    def regenerate(self, parameter_set, template):
        for k in self.fail_on:
            if k in parameter_set:
                raise RuntimeError("CATIA Update failed on parameter '%s' (simulated)" % k)

        os.makedirs(self.out_dir, exist_ok=True)
        fname = "%s_%s.CATPart" % (template.id, datetime.datetime.now().strftime("%H%M%S%f"))
        path = os.path.join(self.out_dir, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# FAKE CATPart (test stub)\n")
            fh.write("template=%s\n" % template.id)
            for k in sorted(parameter_set):
                fh.write("%s=%r\n" % (k, parameter_set[k]))
        return CatPartResult(path, _sha_file(path), dict(parameter_set))


class RealCatiaDriver(CatiaDriver):
    """
    真机：V5 Automation（win32com）。

    前提：本机已安装并授权 CATIA V5（R22/R23 on Win7）。无 headless，启动 CNEXT 进程。
    参数化路径：打开模板 CATPart -> 按参数契约设置 Parameters -> Part.Update() -> SaveAs。
    """

    def __init__(self, out_dir):
        self.out_dir = out_dir

    def regenerate(self, parameter_set, template):
        import win32com.client  # 仅在真机导入
        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir)

        src = template.full_path()
        if not src or not os.path.isfile(src):
            raise FileNotFoundError("模板 CATPart 不存在: %s" % src)

        app = win32com.client.Dispatch("CATIA.Application")
        app.Visible = False
        try:
            doc = app.Documents.Open(src)
            part = doc.Part
            params = part.Parameters
            for name, val in parameter_set.items():
                try:
                    p = params.Item(name)
                    p.Value = val
                except Exception:
                    # 参数不存在则在契约中不应出现；交由上层校验拦截
                    raise RuntimeError("模板缺少参数 '%s'" % name)
            part.Update()  # 循环外一次性 Update（R18+ 经验）

            out = os.path.join(self.out_dir, "%s_%s.CATPart"
                               % (template.id, datetime.datetime.now().strftime("%H%M%S%f")))
            doc.SaveAs(out)
            doc.Close()
            return CatPartResult(out, _sha_file(out), dict(parameter_set))
        finally:
            try:
                app.Quit()
            except Exception:
                pass
