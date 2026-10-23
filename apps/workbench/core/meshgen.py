# -*- coding: utf-8 -*-
"""
meshgen.py -- 有限元网格生成（票 04）

关键约束 ADR-0010：交付网格必须沿长桁线与肋线切分，**跨自然格边界的不一致单元不得
作为最终网格**。所以这里的做法是：**每个自然格独立划分自身网格，格与格之间共用边界
节点**。这样每个单元天然唯一隶属某个自然格，不存在跨界单元。

不连续格（ADR-0008）仍要划分网格（单元级校核照常），只是不参与自然格级聚合；
因此同样为它生成网格，并打上 `bay_id` 与 `bay_valid=False`。

纯算法，不依赖 CATIA / Abaqus。兼容 Python 3.8（与 2.6.2 语法子集兼容，便于移植）。
"""

import json


class Mesh(object):
    """
    结构化壳网格。

    nodes: [(node_id, y, xi)]  参数空间坐标（y 展向, xi 弦向 0..1）
    elems: [{'id':, 'nodes': (n1,n2,n3,n4), 'bay_id':, 'bay_valid':}]
    """

    def __init__(self):
        self.nodes = []
        self.elems = []
        self._node_index = {}   # (rounded_y, rounded_xi) -> node_id
        # 票15：每自然格实际使用的细分数 {bay_id: (n_y, n_xi)}；
        # 质量门加密后靠它反推"当前口径"，不必从单元数猜。
        self.subdiv = {}

    # -- 节点去重：相邻自然格共边时必须复用同一批节点 --
    def node(self, y, xi):
        key = (round(y, 9), round(xi, 9))
        nid = self._node_index.get(key)
        if nid is None:
            nid = len(self.nodes) + 1          # Abaqus 节点从 1 开始
            self.nodes.append((nid, y, xi))
            self._node_index[key] = nid
        return nid

    def add_quad(self, y0, y1, xi0, xi1, bay_id, bay_valid, n_sub_y, n_sub_xi):
        """在 [y0,y1] x [xi0,xi1] 内划 n_sub_y x n_sub_xi 个四边形单元。"""
        ys = [y0 + (y1 - y0) * k / float(n_sub_y) for k in range(n_sub_y + 1)]
        xis = [xi0 + (xi1 - xi0) * k / float(n_sub_xi) for k in range(n_sub_xi + 1)]

        grid = []
        for yy in ys:
            row = []
            for xx in xis:
                row.append(self.node(yy, xx))
            grid.append(row)

        for a in range(n_sub_y):
            for b in range(n_sub_xi):
                n1 = grid[a][b]
                n2 = grid[a][b + 1]
                n3 = grid[a + 1][b + 1]
                n4 = grid[a + 1][b]
                self.elems.append({
                    "id": len(self.elems) + 1,
                    "nodes": (n1, n2, n3, n4),
                    "bay_id": bay_id,
                    "bay_valid": bay_valid,
                })

    def bay_of_elem(self):
        """element_index(0-based) -> bay_id，用于归属查询。"""
        return [e["bay_id"] for e in self.elems]

    def count_by_bay(self):
        counts = {}
        for e in self.elems:
            counts[e["bay_id"]] = counts.get(e["bay_id"], 0) + 1
        return counts

    def to_dict(self):
        return {
            "n_nodes": len(self.nodes),
            "n_elems": len(self.elems),
            "nodes": [{"id": n, "y": y, "xi": xi} for (n, y, xi) in self.nodes],
            "elems": [{"id": e["id"], "nodes": list(e["nodes"]),
                       "bay_id": e["bay_id"], "bay_valid": e["bay_valid"]}
                      for e in self.elems],
            "subdiv": dict((k, list(v)) for k, v in self.subdiv.items()),
        }


def build_mesh(model, subdiv_per_bay=4, subdiv_by_bay=None):
    """
    由 TopologyModel 生成网格：每个自然格独立划 n_y × n_xi，
    相邻格共边复用节点，因此**不存在跨自然格边界的单元**。

    model          : topology.TopologyModel
    subdiv_per_bay : 全局默认每格每方向细分数（P1 口径）
    subdiv_by_bay  : 可选 {bay_id: n} 逐格覆盖（票15 自动加密用）；
                     未列出的格沿用 subdiv_per_bay。
                     值也可以是 (n_y, n_xi) 二元组做**各向异性**加密
                     （长宽比超标时长边方向加倍才有用，见 mesh_quality）。

    **签名保持不变**（票15 只在值上扩展允许类型），故既有调用零回归。
    返回 Mesh；每格实际细分数记在 `mesh.subdiv`。
    """
    mesh = Mesh()
    per_bay = subdiv_by_bay or {}
    order = sorted(model.bays, key=lambda b: (b.rib_j, b.str_i))
    for bay in order:
        spec = per_bay.get(bay.id, subdiv_per_bay)
        if isinstance(spec, (tuple, list)):
            if len(spec) < 2:
                raise ValueError("subdiv_by_bay[%r] 若为序列需 (n_y, n_xi)，实际 %r"
                                 % (bay.id, spec))
            n_y, n_xi = int(spec[0]), int(spec[1])
        else:
            n_y = n_xi = int(spec)
        n_y = max(n_y, 1)
        n_xi = max(n_xi, 1)
        mesh.add_quad(bay.y0, bay.y1, bay.xi0, bay.xi1,
                      bay.id, bay.valid, n_y, n_xi)
        mesh.subdiv[bay.id] = (n_y, n_xi)
    return mesh


def verify_no_straddle(model, mesh):
    """
    校验 ADR-0010：没有任何单元跨越自然格边界。
    因为网格按格划分且共边复用节点，理论上恒成立；此处做防御性断言。
    返回 (ok, problems)
    """
    problems = []
    # 每个单元四个角点必须落在同一格的 [y0,y1]x[xi0,xi1] 闭区间内
    bay_by_id = dict((b.id, b) for b in model.bays)
    node_by_id = dict((n, (y, xi)) for (n, y, xi) in mesh.nodes)
    for e in mesh.elems:
        bay = bay_by_id.get(e["bay_id"])
        if bay is None:
            problems.append("elem %d has unknown bay %r" % (e["id"], e["bay_id"]))
            continue
        for nid in e["nodes"]:
            y, xi = node_by_id[nid]
            tol = 1e-9
            if not (bay.y0 - tol <= y <= bay.y1 + tol
                    and bay.xi0 - tol <= xi <= bay.xi1 + tol):
                problems.append("elem %d node %d outside bay %s"
                                % (e["id"], nid, bay.id))
                break
    return (len(problems) == 0), problems
