# -*- coding: utf-8 -*-
"""
PROTOTYPE / 一次性验证 -- 请勿用于生产。

问题：能否从参数化机翼盒段几何自动、鲁棒地生成"自然格"(Natural Bay)，
      并稳定映射到有限元网格？

自然格定义(用户给定)：
  由相邻两根长桁与相邻两根肋围合而成、传力路径连续不间断的一块蒙皮区域。

本原型的做法：
  1. 在参数空间 (eta=展向, xi=弦向 0..1) 里把结构建成拓扑网格：
     - 长桁 = 等 xi 线(可只覆盖部分展长 -> runout)
     - 肋   = 等 eta 线(可只覆盖部分弦长 -> partial rib)
  2. 候选自然格 = 肋区间 x 长桁区间 的张量积单元。
  3. 对每个候选格做"连续性校验"：四条边界必须都有真实、连续、
     且完整跨越该格区间的长桁/肋；若内部有开口(cutout)则判为不连续。
  4. 生成 FE 网格(结构化)，用形心参数坐标归属到自然格，统计跨边界单元。
  5. 出图：几何 + 长桁/肋 + 自然格着色 + FE 单元归属 + 失效格高亮。

运行： python gen_natural_bays.py
"""

import math
import os
import numpy as np
from PIL import Image, ImageDraw

OUT = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------------
# 1. 参数化机翼盒段几何 (物理坐标: 展向 Y, 弦向 X)
# ----------------------------------------------------------------------------
class WingBox:
    def __init__(self, span=12.0, x_le0=0.0, sweep_deg=25.0,
                 c_root=3.0, c_tip=1.2):
        self.span = span
        self.x_le0 = x_le0
        self.sweep = math.radians(sweep_deg)
        self.c_root = c_root
        self.c_tip = c_tip

    def x_le(self, y):
        """前梁弦向位置随展向平移(后掠)。"""
        return self.x_le0 + y * math.tan(self.sweep)

    def chord(self, y):
        """盒段弦长随展向线性收缩(锥度)。"""
        return self.c_root + (self.c_tip - self.c_root) * (y / self.span)

    def phys(self, y, xi):
        """参数 (y, xi) -> 物理 (X, Y)。xi: 0=前梁, 1=后梁。"""
        X = self.x_le(y) + xi * self.chord(y)
        return X, y


# ----------------------------------------------------------------------------
# 2. 结构布置
# ----------------------------------------------------------------------------
class Layout:
    """stringers: [{'xi':..,'y0':..,'y1':..}]  肋: [{'y':..,'xi0':..,'xi1':..}]"""
    def __init__(self, stringers, ribs, holes=None):
        self.stringers = stringers
        self.ribs = ribs
        self.holes = holes or []


def full_stringers(xi_list, span):
    return [{'xi': xi, 'y0': 0.0, 'y1': span} for xi in xi_list]


def full_ribs(y_list, xi0=0.0, xi1=1.0):
    return [{'y': y, 'xi0': xi0, 'xi1': xi1} for y in y_list]


# ----------------------------------------------------------------------------
# 3. 连续性校验：判断一个候选自然格是否成立
# ----------------------------------------------------------------------------
def _covers(rng, a, b, tol=1e-9):
    """区间 rng=[r0,r1] 是否完整覆盖 [a,b]。"""
    return rng[0] <= a + tol and rng[1] >= b - tol


def bay_validity(box, layout, j, i, tol=1e-9):
    """
    候选格 = 肋 j .. 肋 j+1 (展向 y in [rj,rj+1])
             长桁 i .. 长桁 i+1 (弦向 xi in [si,si+1])
    返回 (valid: bool, reasons:list)
    """
    ribs, strs = layout.ribs, layout.stringers
    rj, rj1 = ribs[j]['y'], ribs[j + 1]['y']
    si, si1 = strs[i]['xi'], strs[i + 1]['xi']
    reasons = []

    # 左/右边界：长桁 i / i+1 必须完整跨越 [rj, rj1]
    for label, s in (('left-stringer', strs[i]), ('right-stringer', strs[i + 1])):
        if not _covers((s['y0'], s['y1']), rj, rj1, tol):
            reasons.append('%s xi=%.3f 未连续跨越展向[%.2f,%.2f] (runout)'
                           % (label, s['xi'], rj, rj1))

    # 下/上边界：肋 j / j+1 必须完整跨越 [si, si1]  (partial rib 检测)
    for label, rib in (('lower-rib', ribs[j]), ('upper-rib', ribs[j + 1])):
        if not _covers((rib['xi0'], rib['xi1']), si, si1, tol):
            reasons.append('%s y=%.3f 未连续跨越弦向[%.3f,%.3f] (partial rib)'
                           % (label, rib['y'], si, si1))

    # 内部开口：任一 hole 落在格内部 -> 传力路径被中断
    for h in layout.holes:
        hy, hxi = h['cy'], h['cxi']
        if rj < hy < rj1 and si < hxi < si1:
            reasons.append('开口(cutout) 位于格内 (y=%.2f, xi=%.3f) -> 不连续'
                           % (hy, hxi))

    return (len(reasons) == 0), reasons


# ----------------------------------------------------------------------------
# 4. FE 网格 + 归属
# ----------------------------------------------------------------------------
def build_fe_mesh(n_y, n_xi):
    """返回形心参数坐标数组 (ny*nxi, 2)，结构化壳网格。"""
    ys = (np.arange(n_y) + 0.5) / n_y
    xis = (np.arange(n_xi) + 0.5) / n_xi
    pts = [(y, xi) for y in ys for xi in xis]
    return np.array(pts, dtype=float)


def assign_bays(box, layout, elems):
    """把每个 FE 单元形心 -> 自然格索引 (j,i)。返回 idx 数组及跨边界单元计数。"""
    rib_ys = [r['y'] for r in layout.ribs]
    str_xis = [s['xi'] for s in layout.stringers]
    n_j, n_i = len(rib_ys) - 1, len(str_xis) - 1

    idx = np.full(len(elems), -1, dtype=int)
    straddle = 0
    for k, (y, xi) in enumerate(elems):
        # 展向所在肋区间
        jj = np.searchsorted(rib_ys, y, side='right') - 1
        ii = np.searchsorted(str_xis, xi, side='right') - 1
        jj = min(max(jj, 0), n_j - 1)
        ii = min(max(ii, 0), n_i - 1)
        idx[k] = jj * n_i + ii

    # 跨边界单元：单元的四个角是否落在不同格
    for k in range(len(elems)):
        y, xi = elems[k]
        pass  # 简化：以形心归属为准；单独统计见下

    # 统计跨边界(形心归属 vs 任一角归属不同)
    dy, dxi = 0.5 / 40.0, 0.5 / 40.0
    for k, (y, xi) in enumerate(elems):
        corners = [(y - dy, xi - dxi), (y - dy, xi + dxi),
                   (y + dy, xi - dxi), (y + dy, xi + dxi)]
        base = idx[k]
        for (cy, cxi) in corners:
            jj = np.searchsorted(rib_ys, cy, side='right') - 1
            ii = np.searchsorted(str_xis, cxi, side='right') - 1
            jj = min(max(jj, 0), n_j - 1)
            ii = min(max(ii, 0), n_i - 1)
            if jj * n_i + ii != base:
                straddle += 1
                break
    return idx, straddle


# ----------------------------------------------------------------------------
# 5. 渲染
# ----------------------------------------------------------------------------
def render(box, layout, elems, idx, valid_map, path, title):
    W, H, M = 1500, 900, 70
    img = Image.new('RGB', (W, H), 'white')
    d = ImageDraw.Draw(img, 'RGBA')

    # 计算物理范围
    xs, ys = [], []
    for y in np.linspace(0, box.span, 20):
        for xi in (0.0, 1.0):
            X, Y = box.phys(y, xi)
            xs.append(X); ys.append(Y)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    def px(X, Y):
        gx = M + (Y - ymin) / (ymax - ymin) * (W - 2 * M)
        gy = M + (X - xmin) / (xmax - xmin) * (H - 2 * M)
        return gx, gy

    ribs, strs = layout.ribs, layout.stringers
    n_j, n_i = len(ribs) - 1, len(strs) - 1

    # 自然格填色
    palette = []
    for k in range(max(1, n_j * n_i)):
        h = (k * 0.61803) % 1.0
        import colorsys
        r, g, b = colorsys.hsv_to_rgb(h, 0.35, 1.0)
        palette.append((int(r * 255), int(g * 255), int(b * 255)))

    for j in range(n_j):
        for i in range(n_i):
            k = j * n_i + i
            y0, y1 = ribs[j]['y'], ribs[j + 1]['y']
            x0, x1 = strs[i]['xi'], strs[i + 1]['xi']
            quad = []
            for (y, xi) in [(y0, x0), (y0, x1), (y1, x1), (y1, x0)]:
                X, Y = box.phys(y, xi)
                quad.append(px(X, Y))
            valid = valid_map.get((j, i), True)
            col = palette[k % len(palette)]
            alpha = 110 if valid else 40
            d.polygon(quad, fill=col + (alpha,))
            if not valid:
                d.line(quad + [quad[0]], fill=(220, 30, 30, 255), width=4)

    # 长桁 / 肋 线
    for s in strs:
        pts = []
        for y in np.linspace(s['y0'], s['y1'], 30):
            X, Y = box.phys(y, s['xi'])
            pts.append(px(X, Y))
        d.line(pts, fill=(20, 20, 20, 220), width=2)
    for r in ribs:
        pts = []
        for xi in np.linspace(r['xi0'], r['xi1'], 30):
            X, Y = box.phys(r['y'], xi)
            pts.append(px(X, Y))
        d.line(pts, fill=(20, 20, 90, 220), width=2)

    # 开口
    for h in layout.holes:
        X, Y = box.phys(h['cy'], h['cxi'])
        cx, cy = px(X, Y)
        rr = 14
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                  fill=(255, 255, 255, 255), outline=(200, 0, 0, 255), width=3)

    # 标题
    d.text((M, 20), title, fill=(0, 0, 0, 255))
    d.text((M, 40),
           '红线=不连续自然格(需拆分/合并)  黑线=长桁  蓝线=肋  红圈=开口',
           fill=(60, 60, 60, 255))

    img.save(path)
    return path


# ----------------------------------------------------------------------------
# 6. 场景
# ----------------------------------------------------------------------------
def run_scenario(name, box, layout, n_y=40, n_xi=24):
    print('=' * 74)
    print('场景:', name)
    print('  展向肋数=%d, 弦向长桁数=%d, 开口数=%d'
          % (len(layout.ribs), len(layout.stringers), len(layout.holes)))

    n_j, n_i = len(layout.ribs) - 1, len(layout.stringers) - 1
    valid_map = {}
    invalid = []
    for j in range(n_j):
        for i in range(n_i):
            ok, reasons = bay_validity(box, layout, j, i)
            valid_map[(j, i)] = ok
            if not ok:
                invalid.append((j, i, reasons))

    elems = build_fe_mesh(n_y, n_xi)
    idx, straddle = assign_bays(box, layout, elems)

    total_cells = n_j * n_i
    print('  候选自然格 = %d x %d = %d ; 有效 = %d ; 失效 = %d'
          % (n_j, n_i, total_cells, total_cells - len(invalid), len(invalid)))
    print('  FE 单元 = %d 个 ; 形心全部归属成功 = %s'
          % (len(elems), bool((idx >= 0).all())))
    print('  跨自然格边界的单元 = %d (%.2f%%)  <- 边界归属规则要明确'
          % (straddle, 100.0 * straddle / len(elems)))
    for (j, i, reasons) in invalid:
        print('    [失效] NB-r%d-s%d:' % (j, i))
        for r in reasons:
            print('           -', r)

    p = render(box, layout, elems, idx, valid_map,
               os.path.join(OUT, 'bay_%s.png' % name),
               '自然格原型 :: %s' % name)
    print('  图:', p)
    return {'name': name, 'total': total_cells, 'invalid': len(invalid),
            'straddle': straddle, 'elems': len(elems)}


def main():
    box = WingBox(span=12.0, sweep_deg=25.0, c_root=3.0, c_tip=1.2)
    results = []

    # A: 干净张量网格(后掠+锥度+非均匀肋距)
    ribsA = [0, 2, 4.5, 6.2, 9, 12]
    strsA = [0.0, 0.15, 0.3, 0.5, 0.7, 0.85, 1.0]
    results.append(run_scenario(
        'A_clean', box, Layout(full_stringers(strsA, box.span), full_ribs(ribsA))))

    # B: 长桁 runout -- 中间某根长桁只覆盖前 60% 展长
    strsB = [dict(s) for s in full_stringers(strsA, box.span)]
    strsB[3]['y1'] = 0.6 * box.span   # xi=0.5 的长桁在 60% 展长终止
    results.append(run_scenario(
        'B_runout', box, Layout(strsB, full_ribs(ribsA))))

    # C: 蒙皮开口 -- 某格内开一个检修口
    results.append(run_scenario(
        'C_cutout', box,
        Layout(full_stringers(strsA, box.span), full_ribs(ribsA),
               holes=[{'cy': 5.3, 'cxi': 0.42}])))

    # D: 部分肋 -- 某根肋只从后梁延伸到 60% 弦长
    ribsD = full_ribs(ribsA)
    ribsD[2] = {'y': ribsA[2], 'xi0': 0.4, 'xi1': 1.0}
    results.append(run_scenario(
        'D_partial_rib', box, Layout(full_stringers(strsA, box.span), ribsD)))

    print('=' * 74)
    print('汇总:')
    for r in results:
        print('  %-16s 候选格=%3d 失效=%2d 跨边界单元=%4d/%d'
              % (r['name'], r['total'], r['invalid'], r['straddle'], r['elems']))


if __name__ == '__main__':
    main()
