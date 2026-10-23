# -*- coding: utf-8 -*-
"""
hotspot_figure.py -- 危险部位标注图（票 07）

用 Pillow 把网格投到参数空间 (y 展向, xi 弦向)，按自然格着色，
危险部位高亮为红色并标注 FI。纯绘图，不依赖 Tk。

兼容 Python 3.8。
"""

import os

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None


_FONT_CACHE = {}


def _font(size=14):
    """
    加载可显示中文的字体。
    Win7/Win10 常见中文字体路径；找不到则退回 Pillow 默认字体
    （此时中文会显示为方框，但不影响程序正确性）。
    """
    key = int(size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑（Win7+）
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",    # 黑体（Win7 自带）
        r"C:\Windows\Fonts\simsun.ttc",    # 宋体
        r"C:\Windows\Fonts\simkai.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = f
                return f
            except Exception:
                continue

    try:
        f = ImageFont.load_default(size)
    except Exception:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    _FONT_CACHE[key] = f
    return f


def draw_hotspot_figure(result, out_png, width=1100, height=700):
    """
    绘制危险部位俯视图。

    result : run_study 返回的 dict（需含 topology_model / fe_model / check）
             也接受直接传入 {'topology_model':..., 'fe_model':..., 'check':...}
    out_png: 输出路径
    """
    if Image is None:
        raise RuntimeError("Pillow 未安装，无法绘图")

    model = result.get("topology_model")
    fe = result.get("fe_model")
    chk = result.get("check") or {}
    hotspots = chk.get("hotspots") or []
    invalid_ids = set(chk.get("invalid_bays") or [])

    margin_l, margin_r, margin_t, margin_b = 90, 40, 60, 70
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    f_title = _font(18)
    f_label = _font(13)
    f_small = _font(11)

    # 确定参数空间范围
    if model is not None and model.bays:
        y_min = min(b.y0 for b in model.bays)
        y_max = max(b.y1 for b in model.bays)
        xi_min = min(b.xi0 for b in model.bays)
        xi_max = max(b.xi1 for b in model.bays)
    elif fe is not None:
        ys = [n[1] for n in fe.mesh.nodes]
        xis = [n[2] for n in fe.mesh.nodes]
        y_min, y_max = min(ys), max(ys)
        xi_min, xi_max = min(xis), max(xis)
    else:
        y_min, y_max, xi_min, xi_max = 0.0, 1.0, 0.0, 1.0

    dy = (y_max - y_min) or 1.0
    dxi = (xi_max - xi_min) or 1.0

    def px(y, xi):
        gx = margin_l + (y - y_min) / dy * plot_w
        gy = margin_t + (xi - xi_min) / dxi * plot_h
        return (gx, gy)

    # ---- 自然格填色 ----
    if model is not None:
        for bay in model.bays:
            x0, y0 = px(bay.y0, bay.xi0)
            x1, y1 = px(bay.y1, bay.xi1)
            if bay.id in invalid_ids:
                fill = (245, 235, 235)
                outline = (200, 120, 120)
            elif bay.valid:
                fill = (238, 244, 250)
                outline = (150, 175, 200)
            else:
                fill = (245, 235, 235)
                outline = (200, 120, 120)
            d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                        fill=fill, outline=outline)

    # ---- 单元形心与热点 ----
    # 只把真正"危险"的单元标红：FI 超过阈值或属于前 3 名
    # （校核结果里的 hotspots 是排序后的 Top-N，用于报告表格；图上标红需更克制）
    ranked = sorted(hotspots, key=lambda h: -(h.get("FI") or 0.0))
    top_n = set(h["elem"] for h in ranked[:3])
    hot_by_elem = {}
    for h in hotspots:
        fi = h.get("FI") or 0.0
        if h["elem"] in top_n or fi >= 1.0:
            hot_by_elem[h["elem"]] = h

    if fe is not None and model is not None:
        node_by_id = dict((n, (y, xi)) for (n, y, xi) in fe.mesh.nodes)
        for e in fe.mesh.elems:
            pts = [node_by_id[nid] for nid in e["nodes"] if nid in node_by_id]
            if not pts:
                continue
            cy = sum(p[0] for p in pts) / float(len(pts))
            cxi = sum(p[1] for p in pts) / float(len(pts))
            cx, cyy = px(cy, cxi)
            h = hot_by_elem.get(e["id"])
            if h is not None:
                r = 9
                d.ellipse([cx - r, cyy - r, cx + r, cyy + r],
                          fill=(220, 40, 40), outline=(120, 0, 0))
                txt = "FI=%.3g" % h["FI"]
                d.text((cx + r + 3, cyy - 6), txt, fill=(160, 0, 0), font=f_small)
            else:
                d.ellipse([cx - 1.5, cyy - 1.5, cx + 1.5, cyy + 1.5],
                          fill=(120, 140, 160))

    # ---- 坐标轴 ----
    d.rectangle([margin_l, margin_t, margin_l + plot_w, margin_t + plot_h],
                outline=(80, 80, 80))
    d.text((margin_l, margin_t - 26), "xi (弦向 0=前梁 → 1=后梁)",
           fill=(40, 40, 40), font=f_label)
    d.text((margin_l - 70, margin_t + plot_h + 18), "y (展向)",
           fill=(40, 40, 40), font=f_label)

    # ---- 标题与图例 ----
    d.text((margin_l, 18), "危险部位分布（红色 = 危险部位，含 FI 标注）",
           fill=(20, 20, 20), font=f_title)
    ly = height - 46
    d.ellipse([margin_l, ly, margin_l + 12, ly + 12], fill=(220, 40, 40))
    d.text((margin_l + 18, ly - 1), "危险部位", fill=(60, 60, 60), font=f_label)
    d.rectangle([margin_l + 120, ly, margin_l + 132, ly + 12],
                fill=(238, 244, 250), outline=(150, 175, 200))
    d.text((margin_l + 138, ly - 1), "有效自然格", fill=(60, 60, 60), font=f_label)
    d.rectangle([margin_l + 250, ly, margin_l + 262, ly + 12],
                fill=(245, 235, 235), outline=(200, 120, 120))
    d.text((margin_l + 268, ly - 1), "不连续格(已排除)", fill=(60, 60, 60), font=f_label)

    d.text((margin_l + 430, ly - 1),
           "单元数 %d；热点 %d 处" % (
               len(fe.mesh.elems) if fe is not None else 0, len(hotspots)),
           fill=(90, 90, 90), font=f_label)

    out_dir = os.path.dirname(os.path.abspath(out_png))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    img.save(out_png)
    return out_png
