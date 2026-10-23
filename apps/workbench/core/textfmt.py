# -*- coding: utf-8 -*-
"""
textfmt.py -- 纯文本渲染的小工具（P2 复核提取）

服务层（workbench_service）与型号汇总层（model_rollup）都要把同样的数字/判定
渲染成人可读文本。首轮实现把 `_json_loads` / `_fmt_num` / `_fmt_verdict` 各写了
一份（code-review 的 Duplicated Code 项）——两处漂移只是时间问题，故收敛到这里。

纯标准库。兼容 Python 3.8。
"""

import json


def json_loads(text, default=None):
    """宽松 JSON 解析：None/坏数据都返回 default，不抛。"""
    if text is None:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def fmt_num(value, fmt="%.4f"):
    """数值格式化；None -> "-"，不可转 -> str(value)。"""
    if value is None:
        return "-"
    try:
        return fmt % float(value)
    except (TypeError, ValueError):
        return str(value)


def fmt_verdict(passed):
    """合格判定文案；三态（None = 无结论）。"""
    if passed is None:
        return "无结论"
    return "合格" if passed else "不合格"


def fmt_row(cells, widths=None):
    """
    按固定列宽拼一行（表格文本共用，避免两处各写一个格式串）。

    cells   : 值序列
    widths  : 各列宽度（None 则取 12）
    """
    cols = [str(c) for c in cells]
    if widths is None:
        widths = [12] * len(cols)
    out = []
    for i, c in enumerate(cols):
        w = widths[i] if i < len(widths) else 12
        out.append(("%-" + str(w) + "s") % c)
    return "  ".join(out).rstrip()
