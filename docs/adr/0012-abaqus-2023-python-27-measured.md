# 实测环境事实：Abaqus 2023 + Python 2.7.15（取代原 ADR-0003 的 6.13/2.6.2 假设）

**Status: accepted（实测取代假设）**

## 背景

原 ADR-0003 基于"现场使用 Abaqus 6.13-4，内置 Python 2.6.2"的陈述，要求所有 Abaqus 侧脚本按 **2.6.2 语法**编写。该陈述来自早期需求描述与二手资料核查，**未经真机验证**。

## 实测结果（2026-10-23，目标机）

```
D:\abaqus2023\Commands\abaqus.bat information=release
  -> Abaqus 2023   (BUILD 2022_09_29-02.11.55)
abaqus python -c "import sys; print(sys.version)"
  -> 2.7.15 (default, Aug 27 2022, 06:47:47) [MSC v.1928 64 bit (AMD64)]
```

- 内置 Python 是 **2.7.15**，不是 2.6.2。
- `odbAccess`、`abaqusConstants` 均可导入。
- 真机求解成功：`abaqus job=plate inp=model.inp interactive` → `Abaqus JOB plate COMPLETED`。
- ODB 读取成功：能取到 `S`（含 `sectionPoint.number` 铺层区分）与 `U`，2 层铺层实测 section points = [1, 6]。

## 决定

Abaqus 侧脚本以 **Python 2.7** 为基线（不是 2.6.2）：

- 允许：`dict`/`set` 推导、`str.format`、`print()` 函数式、`with` 多上下文、`OrderedDict`。
- 仍禁止（2.7 也没有）：f-string（3.6+）、`yield from`、`nonlocal`、变量/参数注解、`async`。
- `compat_check.py` 的检查规则**保持不变**——它拦的正是 3.x-only 特性，对 2.7 而言依然全部无效，因此仍然是有价值的门槛（只是不再需要禁用 2.7 特性）。

## Consequences

- ADR-0003 的"2.6.2 限制"失效；本 ADR 取代其关于**版本**的结论，但其"宿主与 Abaqus 只交换 JSON/CSV 文本、不传对象"的结论**依然有效**（跨解释器 3.8 ↔ 2.7 更需要文本边界）。
- 现场若仍有其他机器跑 6.13-4，则兼容基线回落到该机的 2.6.2；**以现场 `abaqus python` 实测为准**，不要假设。
- 设计共识与 P1 技术选型中的"6.13-4 / 2.6.2"表述需同步更正。
