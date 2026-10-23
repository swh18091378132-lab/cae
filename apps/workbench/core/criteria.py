# -*- coding: utf-8 -*-
"""
criteria.py -- 复合材料失效准则库（票 06）

纯算法，确定性（L0）。对应 ADR-0007：FI/MS 的计算与判定永不由 LLM 完成。

术语（GLOSSARY）：
  FI 失效指数 = 施加/许用，FI >= 1 判失效
  MS 安全裕度 = 许用/施加 - 1，MS >= 0 满足要求

约定：本模块输入**应力/应变分量**与**材料许用值**，输出 FI。
多轴准则按各自公式计算；材料强度符号约定：
  Xt 纵向拉伸, Xc 纵向压缩(正数), Yt 横向拉伸, Yc 横向压缩(正数), S 面内剪切

兼容 Python 3.8（宿主侧运行）。
"""

import math


class CriteriaError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 单轴/简单准则
# ---------------------------------------------------------------------------

def max_stress(s, allow):
    """
    最大应力准则：取各分量应力/许用 的最大比值。
    s: {'s11','s22','s12'} ; allow: {'Xt','Xc','Yt','Yc','S'}
    """
    fi = 1e-12
    fi = max(fi, _ratio_tension_compression(s["s11"], allow["Xt"], allow["Xc"]))
    fi = max(fi, _ratio_tension_compression(s["s22"], allow["Yt"], allow["Yc"]))
    fi = max(fi, abs(s["s12"]) / float(allow["S"]))
    return fi


def max_strain(e, allow):
    """最大应变准则（输入应变与应变许用）。"""
    fi = 1e-12
    fi = max(fi, _ratio_tension_compression(e["e11"], allow["ext"], allow["exc"]))
    fi = max(fi, _ratio_tension_compression(e["e22"], allow["eyt"], allow["eyc"]))
    fi = max(fi, abs(e["e12"]) / float(allow["es"]))
    return fi


def _ratio_tension_compression(v, allow_t, allow_c):
    if v >= 0:
        return v / float(allow_t)
    return abs(v) / float(allow_c)


# ---------------------------------------------------------------------------
# 二次型准则
# ---------------------------------------------------------------------------

def tsai_hill(s, allow):
    """
    Tsai-Hill（单式、不分拉压）：
      FI = sqrt( (s11/X)^2 - (s11*s22/X^2) + (s22/Y)^2 + (s12/S)^2 )
    简化取 X=Xt (受拉)/(Xc) 视符号；此处按行业常用：X 取与 s11 同号者。
    """
    X = allow["Xt"] if s["s11"] >= 0 else allow["Xc"]
    Y = allow["Yt"] if s["s22"] >= 0 else allow["Yc"]
    s11, s22, s12 = s["s11"], s["s22"], s["s12"]
    val = ((s11 / float(X)) ** 2
           - (s11 * s22) / float(X * X)
           + (s22 / float(Y)) ** 2
           + (s12 / float(allow["S"])) ** 2)
    return math.sqrt(max(val, 0.0))


def tsai_wu(s, allow, f12_star=-0.5):
    """
    Tsai-Wu（含双轴项 F12）：
      F1*s11 + F2*s22 + F11*s11^2 + F22*s22^2 + 2*F12*s11*s22 + F66*s12^2 = 1
      F1 = 1/Xt - 1/Xc ; F2 = 1/Yt - 1/Yc
      F11 = 1/(Xt*Xc)  ; F22 = 1/(Yt*Yc) ; F66 = 1/S^2
      F12 = f12_star * sqrt(F11*F22)   (f12_star 常用 -0.5)

    FI 的口径（与 max_stress 一致）：**施加量 / 许用量**，即把当前应力放大 FI 倍
    恰好达到失效面。设缩放系数 t 满足 b*t^2 + a*t - 1 = 0，则 FI = 1/t。

      a = F1*s11 + F2*s22                    （线性项）
      b = F11*s11^2 + F22*s22^2 + 2F12*s11*s22 + F66*s12^2  （二次项）
      t = (-a + sqrt(a^2 + 4b)) / (2b)
      FI = 1/t = 2b / (-a + sqrt(a^2 + 4b))
    """
    Xt, Xc = float(allow["Xt"]), float(allow["Xc"])
    Yt, Yc = float(allow["Yt"]), float(allow["Yc"])
    S = float(allow["S"])
    s11, s22, s12 = s["s11"], s["s22"], s["s12"]

    F1 = 1.0 / Xt - 1.0 / Xc
    F2 = 1.0 / Yt - 1.0 / Yc
    F11 = 1.0 / (Xt * Xc)
    F22 = 1.0 / (Yt * Yc)
    F66 = 1.0 / (S * S)
    F12 = f12_star * math.sqrt(F11 * F22)

    a = F1 * s11 + F2 * s22
    b = (F11 * s11 * s11 + F22 * s22 * s22
         + 2.0 * F12 * s11 * s22 + F66 * s12 * s12)

    if b <= 0:
        return abs(a)

    # 缩放系数 t（应力放大多少倍到失效），FI = 1/t
    denom = -a + math.sqrt(a * a + 4.0 * b)
    if denom <= 0:
        return 0.0
    t = denom / (2.0 * b)
    if t <= 0:
        return 0.0
    return 1.0 / t


def hashin(s, allow):
    """
    Hashin 判据（2D，按失效模式分式，取各模式最大值）：
      纤维拉伸 (s11>=0): (s11/Xt)^2 + (s12/S)^2
      纤维压缩 (s11<0) : (s11/Xc)^2
      基体拉伸 (s22>=0): (s22/Yt)^2 + (s12/S)^2
      基体压缩 (s22<0) : (s22/(2S))^2 + ((Yc/(2S))^2 - 1)*(s22/Yc) + (s12/S)^2
    """
    Xt, Xc = float(allow["Xt"]), float(allow["Xc"])
    Yt, Yc = float(allow["Yt"]), float(allow["Yc"])
    S = float(allow["S"])
    s11, s22, s12 = s["s11"], s["s22"], s["s12"]

    if s11 >= 0:
        fi_f = (s11 / Xt) ** 2 + (s12 / S) ** 2
    else:
        fi_f = (s11 / Xc) ** 2

    if s22 >= 0:
        fi_m = (s22 / Yt) ** 2 + (s12 / S) ** 2
    else:
        fi_m = ((s22 / (2.0 * S)) ** 2
                + ((Yc / (2.0 * S)) ** 2 - 1.0) * (s22 / Yc)
                + (s12 / S) ** 2)
    return math.sqrt(max(fi_f, fi_m, 0.0))


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

REGISTRY = {
    "max_stress": max_stress,
    "max_strain": max_strain,
    "tsai_hill": tsai_hill,
    "tsai_wu": tsai_wu,
    "hashin": hashin,
}

# 默认准各向同性/碳纤维典型许用值（MPa），可被规则覆盖
DEFAULT_ALLOWABLES = {
    "Xt": 1500.0, "Xc": 1200.0,
    "Yt": 50.0, "Yc": 200.0,
    "S": 70.0,
    "ext": 0.010, "exc": 0.008,
    "eyt": 0.005, "eyc": 0.020,
    "es": 0.015,
}


def get(name):
    fn = REGISTRY.get(name)
    if fn is None:
        raise CriteriaError("未知失效准则: %r（可选: %s）"
                            % (name, ", ".join(sorted(REGISTRY))))
    return fn
