## 问题

生成的 Abaqus `.inp` **量纲不自洽**：长度用米、壳厚用毫米、模量用 MPa，三者混在一个模型里。

## 证据

模板 `data/templates/wingbox_v1.json` 声明长度单位是**米**：
```json
{"name": "span",  "unit": "m", "default": 12.0}
{"name": "c_root","unit": "m", "default": 3.0}
```

`fe_build.write_inp` 把节点坐标**原样**写入（`x = (xi - xi_min) * c_root`），所以 `c_root=1.0` 时写出：
```
*NODE
1, 0, 0, 0.
2, 0.333333333, 0, 0.
3, 0, 2.5, 0.
```
→ 弦长 1.0、展长区间 2.5。

同一模型的壳厚与模量：
```
*SHELL SECTION, ELSET=ALL, COMPOSITE, OFFSET=0.
0.1, 3, CFRP, 0          ← 0.1（平台按 mm 理解）
*ELASTIC, TYPE=LAMINA
135000, 10000, 0.3, ...  ← MPa
```

而 `extract.py` 的 manifest 明确声称单位制是 **mm-N-MPa**：
```python
"units_note": "Abaqus 模型单位制由建模端决定，P1 使用 mm-N-MPa",
```

三者互相矛盾。无论按哪一套解释都错：
- 若按 mm 理解 → 弦长 1 mm，模型尺度荒谬
- 若按 m 理解 → 壳厚 0.1 m（10 cm 蒙皮）、E=135000 Pa，完全不对

## 影响

1. **应力结果量级错误**：几何用 m、模量用 MPa 时 Abaqus 算出的应力不是 MPa，
   而 `strength_check` 拿它除以 MPa 量级的许用值（`Xt=1500` 等）得到 **FI/MS 不可信**。
   这条直接动摇 P1 起就宣称的"强度校核确定性可信"。
2. **质量计算口径对不上模型**：P3 的 `objectives.mass()` 在 SI 下自洽（米 + 米 + kg/m³），
   但它描述的几何与 `.inp` 里实际求解的几何不是同一个尺度。

## 建议修法（需人决策，不宜顺手改）

统一到 **mm-N-MPa**（Abaqus 复材惯例），或统一到 **m-kg-Pa**，二选一，然后：
- 模板 `unit` 声明与实际写入一致；
- `fe_build` 写入前显式做单位换算（而不是"参数是多少就写多少"）；
- `extract.py` 的 `units_note` 与实际情况一致；
- `objectives` 的质量计算用同一套基准复核。

## 为什么单独开票

这是 **P1 遗留问题**，P1/P2 的既有测试全部通过但从未校验量纲（没有测试断言过
"写进 .inp 的长度与厚度在同一单位制下"）。在 P3 顺手改会悄悄改变 P1/P2 的基线数字，
属于应当显式决策的变更，不适合混在优化闭环的实现里。

发现于 P3 票21（`objectives` 质量口径）实现过程中：该票需要判定"层合板总厚是 mm 还是 m"，
追查后发现平台自身没有统一答案。
