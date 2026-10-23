# Windows 7 (64-bit / SP1) 工具链版本事实核查报告

> 目标：为"最小体积、Windows 7 SP1 x64 桌面端"的 Python 工作台，逐项确认还能在 Win7 上运行的**确切版本号**。
> 约定：**CONFIRMED** = 有一手/官方来源（附 URL）；**UNCERTAIN** = 推断或仅二手来源。
> 时间基准：2026-10（查证时点）。

---

## 1) Python

- **最后一个官方支持 Windows 7 的 CPython = 3.8.x —— CONFIRMED。**
  - 3.8 官方文档原文："Python 3.8 supports Windows Vista and newer"（PEP 11 规则：某 Python 版本仅在对应 Windows 处于微软扩展支持期时支持该平台）——<https://docs.python.org/3.8/using/windows.html>
  - 3.9.0 发行说明原文："The installer now also actively disallows installation on Windows 7. Python 3.9 is incompatible with this unsupported version of Windows." ——<https://www.python.org/downloads/release/python-390/>
  - 故 3.9+ 起硬性拒绝在 Win7 安装，判断成立。

- **带安装包（MSI/EXE installer）的最后一个 3.8 = 3.8.10 —— CONFIRMED。**
  - 3.8.20 页面原文："**Python 3.8.10** was the last full _bugfix release_ of Python 3.8 with binary installers."（3.8.11–3.8.20 仅源码安全更新，不再提供二进制安装包）——<https://www.python.org/downloads/release/python-3820/>
  - 安装包形态：现代 CPython 的 Windows 官方发行是 **.exe 安装器**（不是裸 MSI），另有 **embeddable zip**（精简内嵌版，无 tcl/tk、无 pip）。3.8.8 文件清单可见 `python-3.8.8-amd64.exe` 与 `python-3.8.8-embed-amd64.zip` ——<https://www.python.org/downloads/release/python-388/>
  - ⚠️ Win7 特定前置：**3.8 在 Win7 上需要安装 KB2533623 更新**（embeddable 版不会自检，可能运行期失败）——同上 3.8 文档。

- **3.8 是否仍有安全补丁？→ 否，已 EOL —— CONFIRMED。**
  - 3.8 于 **2024-10-07 到达 end-of-life**，不再接收任何更新 ——<https://devguide.python.org/versions/>；<https://www.python.org/downloads/release/python-388/>（页面顶部 EOL 警告）。

- **3.8 是否自带可用的 tkinter？→ 是（完整安装器）—— CONFIRMED。**
  - 完整安装器的 `Include_tcltk` 选项默认 = 1，安装 Tcl/Tk 支持与 IDLE ——<https://docs.python.org/3.8/using/windows.html>（Installer options 表）。
  - ⚠️ **embeddable 包不含 Tcl/tk**（"Tcl/tk (including all dependants, such as Idle), pip ... are not included"）——同页；若选 embeddable 需自行补 tkinter。

---

## 2) Node.js

- **最后一个能跑 Windows 7 的 Node 大版本 = Node 13（同时也含 Node 12）—— CONFIRMED。Node 14 起要求 Win8.1。**
  - Node **12.x**（LTS "Erbium"）`BUILDING.md` 平台表：`Windows | x64, x86 (WoW64) | >= Windows 7/2008 R2/2012 R2 | Tier 1`——<https://raw.githubusercontent.com/nodejs/node/v12.22.12/BUILDING.md>
  - Node **13.x** 同表同样为 `>= Windows 7/2008 R2/2012 R2`（Tier 1）——<https://raw.githubusercontent.com/nodejs/node/v13.14.0/BUILDING.md>
  - Node **14.0.0** 平台表已改为 `>= Windows 8.1/2012 R2`（Win7 被移除）——<https://raw.githubusercontent.com/nodejs/node/v14.0.0/BUILDING.md>
  - 生命周期（版本发布表）：v12 首发 2019-04，末版 2022-04；v13 首发 2019-10，末版 2020-04（非 LTS，短命）——<https://nodejs.org/en/about/previous-releases>

- **结论**：能用且仍在维护窗口内最久的 Win7 Node = **Node 12（LTS）**；Node 13 虽支持但为非 LTS 且早已 EOL。
- **⚠️ 对 UI 的影响 —— CONFIRMED 逻辑推断**：Electron 当前所有受支持版本都基于 Node 14+/Chromium 新版，**无法在 Win7 运行**；pnpm 也会因 Node 版本过低而不可用。**→ Win7 上应排除 Electron / pnpm / 任何现代 Node 前端栈。**

---

## 3) .NET

- **.NET Framework 4.8 可在 Win7 SP1 上安装运行 —— CONFIRMED。**
  - 官方系统要求表："**Windows 7 SP1** ... 可单独安装：.NET Framework 4.6.2 / 4.7 / 4.7.1 / 4.7.2 / **4.8**"——<https://learn.microsoft.com/en-us/dotnet/framework/get-started/system-requirements>

- **.NET Core 3.1 支持 Win7 SP1 —— CONFIRMED。**
  - `3.1-supported-os.md`：`Windows Client | 7 SP1+, 8.1 | x64, x86`——<https://raw.githubusercontent.com/dotnet/core/v3.1.0/release-notes/3.1/3.1-supported-os.md>

- **重要更正：.NET 5 与 .NET 6 也支持 Win7 SP1（并非"5+ 就不支持"）—— CONFIRMED。**
  - `5.0-supported-os.md`：`Windows Client | 7 SP1(*), 8.1 | x64, x86`，脚注：Win7 SP1 需装 ESU（扩展安全更新）——<https://raw.githubusercontent.com/dotnet/core/v5.0.0/release-notes/5.0/5.0-supported-os.md>
  - 官方 .NET on Windows 安装文档原文："There is no longer a version of .NET that's supported on Windows 7 and Windows 8.1. **The last supported releases was .NET 6 and support ended on November 12, 2024.**"——<https://learn.microsoft.com/en-us/dotnet/core/install/windows>

- **.NET 7+ 确实不再支持 Win7 —— CONFIRMED。**
  - `7.0-supported-os.md` 的"当前支持 Windows"表不含 Win7；Win7 SP1 仅出现在 **Out of support OS versions** 表（2020-01-14 失效）——<https://raw.githubusercontent.com/dotnet/core/main/release-notes/7.0/supported-os.md>

- **正确边界**：**.NET Core 3.1 / .NET 5 / .NET 6 支持 Win7 SP1；.NET 7 起不支持。** 用户的".NET 5+ 不支持"说法**只对 7+ 成立**，对 5/6 不成立。
  - 注：.NET 5/6 在 Win7 上需额外安装 VC++ 2015-2019 Redistributable、KB3063858 等前置——<https://learn.microsoft.com/en-us/dotnet/core/install/windows>

---

## 4) CATIA V5 在 Win7 64-bit

- **官方 Win7 64-bit 支持区间 ≈ V5-6R2013(R23) – V5-6R2018(R28) —— CONFIRMED（二手，来源为 DS Program Directory 的整理）。**
  - 青腾国际对照表（标注数据源为 Dassault Systèmes 官方 Program Directory）：
    - **R23 (V5-6R2013, 2013)**：Win7 ✔、Win8 ✔，架构 **32-bit / 64-bit**
    - **R24 (V5-6R2014, 2014)**：Win7 ✔、Win8 ✔，**限 64-bit**
    - **R25 (V5-6R2015, 2015)**：Win7 ✔、Win8.1 ✔，限 64-bit
    - R26(R2016)–R28(R2018)：Win7/8.1 继续 ✔，限 64-bit
    - **R29(R2019) 起 Win7 不再支持**（仅 Win8.1/10）
  ——<https://tw-qingteng.com/CATIA_V5/catia-windows.html>
- **R23 (2013) 支持 Win7 —— CONFIRMED（同上）。** **R24 / R25 也支持 Win7 —— CONFIRMED（同上，二手）。**
- **R22 (V5-6R2012) 是否支持 Win7 —— UNCERTAIN**（上表自 R23 起列；旧调研显示 R22 支持 Win8，但 R22 明确列 Win7 的一手 LPS 未取得）。
- ⚠️ 权威一手应为 DS 官方 Program Directory（登录墙）：<https://media.3ds.com/support/progdir/all/?pdir=v5,r2017,sp6&context=onpremises>；本结论基于其二手整理。若需法务级确认，须登录 Program Directory 逐版核对 LPS。
- **实践建议**：Win7 上 CATIA V5 版本取 **R23–R28**；本仓库既有约定（R18/R23 时代）中，**R23 是 Win7 的最低务实版本**。

---

## 5) Abaqus 6.13-4

- **官方支持 Windows 7 64-bit —— CONFIRMED（Qualified 平台）。**
  - UNIVERSITY OF ILLINOIS WebStore（SIMULIA 研究版授权分销，逐条列 OS 支持等级）原文：
    - `Windows 7 Professional SP1 is a Qualified platform`
    - `Windows 7 Professional SPx, where x>1, is a Compatible platform`
    - `Windows 8 is a Qualified platform`；`Windows 8.1 is a Validated platform`
    —<https://webstore.illinois.edu/shop/product.aspx?zpid=3041>
  - 3DS SIMULIA 官方社区公告（2013-04）说明 6.13 起"停止支持 Windows 32-bit、Windows XP、Windows Vista"，即 6.13 继续支持 **64-bit Windows（含 Win7）**——<https://3dswym.3dexperience.3ds.com/post/simulia-community/changes-to-support-for-windows-with-abaqus-6-13-isight-5-8-v6-r2014_3405>
- **版本构成**：Abaqus **6.13-4** 为该系列的 Service Pack 04（2014-03 前后发布），仅 Win/Linux x64——<https://www.cg-ku.com/library/nf149686/index.html>
- **内部 Python = 2.6.2**（非 2.7.x）——CONFIRMED（本仓库既有报告 `research_cae_platform_grounding.md` §2 已核实；多方一致：6.12/6.13=2.6.2，6.14/2016 起才升 2.7.3）。
- **已知问题 / 注意点**：
  - 6.13 起**只发 64-bit 安装包**（无 Win32）——须装 x64 版（上文 3DS 公告）。
  - 脚本须按 **Python 2.6.2 语法/库**编写（`print` 语句、无 f-string、旧式异常语法）；`matplotlib` 受 tkinter 版本限制。
  - 需连 **FLEXnet/DSLS 许可服务器**才能提交作业；Abaqus 无内置队列。
  - 中文路径/非 ASCII 路径易出问题（安装器本身即要求纯 ASCII 路径）。

---

## 6) Python GUI / 桌面方案（Win7 + Python 3.8）

| 方案 | 可用版本 | Win7 可行性 | 依据 |
|---|---|---|---|
| **tkinter（标准库）** | 随 3.8 完整安装器 | **最稳、零额外依赖** | CONFIRMED：`Include_tcltk` 默认=1——<https://docs.python.org/3.8/using/windows.html> |
| **PyQt5** | **5.15.11**（最后支持 py3.8） | 可用（Qt5.15 支持 Win7），但体积大 | CONFIRMED 版本：`requires_python>=3.8`，含 `cp38-abi3-win_amd64` 轮子——<https://pypi.org/pypi/PyQt5/5.15.11/json>；Win7 运行性 UNCERTAIN（Qt5.15 官方支持 Win7，推断可行） |
| **本地 Web UI（浏览器）** | Python `http.server`/Flask + 浏览器 | 推荐，UI 最现代、体积可控 | 见下方浏览器 |

- **浏览器（Win7 最后可用版本）**：
  - **Google Chrome 109** 是最后一个支持 Win7/8.1 的 Chrome；109 于 **2023-01-10** 发布、2023-02-07 结束安全支持——<https://endoflife.date/chrome>（数据接口 `https://endoflife.date/api/v1/products/chrome`）。旁证：Microsoft 官方明确 **Edge 109（Chromium 109 内核）是最后一个支持 Win7/8.1 的浏览器版本**——<https://learn.microsoft.com/en-us/lifecycle/products/windows-7>。
  - **Firefox ESR 115** 官方系统要求明确列出 **Windows 7**（"Operating Systems (32-bit and 64-bit): Windows 7 / 8 / 8.1 / 10"）——<https://www.firefox.com/en-US/firefox/115.0esr/system-requirements/>。→ **推荐用 Firefox ESR 115 作为 Win7 本地 Web UI 的宿主浏览器**（ESR 支持周期长、Win7 明确在列）。
- **建议**：最小体积方案 = **tkinter（内置）**；若要"现代观感"且可接受"起本地 http 服务"，用 **Python 3.8 起 `http.server` + Firefox ESR 115**，避开 Electron/Node。

---

## 7) Python 库（Python 3.8 / Win7）

| 库 | 最后一个支持 py3.8 的版本 | Win 轮子 | 依据 |
|---|---|---|---|
| **numpy** | **1.24.4**（1.25 起要求 ≥3.9） | `cp38-win_amd64` ✔ | `requires_python>=3.8`，含 cp38 win_amd64——<https://pypi.org/pypi/numpy/1.24.4/json> |
| **pandas** | **2.0.3**（2.1.0 起要求 ≥3.9） | `cp38-win_amd64` ✔ | 2.0.3 `requires_python>=3.8` 且含 cp38 wheel——<https://pypi.org/pypi/pandas/2.0.3/json>；2.1.0 为 `>=3.9`——<https://pypi.org/pypi/pandas/2.1.0/json> |
| **openpyxl** | **3.1.5** | 纯 Python（any） | `requires_python>=3.8`——<https://pypi.org/pypi/openpyxl/3.1.5/json> |
| **python-docx** | **1.1.2** | 纯 Python + lxml | `requires_python>=3.7`——<https://pypi.org/pypi/python-docx/1.1.2/json> |
| **lxml**（docx 依赖） | **4.9.4**（4.x 末，含 cp38 win 轮子） | `cp38-win_amd64` ✔ | 含 cp38 win_amd64——<https://pypi.org/pypi/lxml/4.9.4/json> |
| **pywin32**（CATIA COM） | **306 / 307 / 308**（311+ 要求 ≥3.9） | `cp38-win_amd64` ✔ | 306 为最后带 .exe 安装器版本且含 cp38——<https://pypi.org/pypi/pywin32/306/json>；308 仍含 cp38 win_amd64——<https://pypi.org/pypi/pywin32/308/json>；最新 312 要求 `>=3.9`——<https://pypi.org/pypi/pywin32/json> |

- **陷阱（务必注意）**：
  1. **VC++ 运行库 / UCRT**：numpy、pandas、lxml、pywin32 的 Windows 轮子都依赖 MSVC 2015+ 运行库；Win7 需预装 **Microsoft Visual C++ 2015-2019 Redistributable** 与 **Universal C Runtime（KB2999226）**，否则 `ImportError: DLL load failed`。
  2. **不要引入 pyarrow/parquet**：pandas 的 `to_parquet` 需 `pyarrow>=7`，体积大且额外依赖；最小体积场景请用 **SQLite / CSV / JSON**。
  3. **pandas 2.x 与 1.x 行为差异**：2.0 起有较多 API/默认值变更；若沿用旧脚本，考虑 pandas **1.5.3**（仍支持 3.8、含 cp38 轮子）以降低改造成本。
  4. **numpy 上限**：numpy 1.25+ 已不支持 3.8，pip 若不加约束会解析失败或装到 3.8 不兼容版本；务必显式 pin `numpy==1.24.4`。
  5. **pywin32 后装脚本**：`pip install` 后需按需运行 `python Scripts/pywin32_postinstall.py -install`（COM 注册）；且不要与系统里其它 `pywintypesXX.dll` 冲突。

---

## 8) SQLite

- **Python 3.8 标准库 `sqlite3` 在 Win7 上无问题 —— CONFIRMED。**
  - `sqlite3` 是随 CPython 编译内置的 C 库，DB-API 2.0，纯本地文件、无独立服务进程，无额外系统依赖——<https://docs.python.org/3.8/library/sqlite3.html>
  - 无 Win7 特有坑（不涉及网络/COM/UCRT 额外依赖；其 DLL 随 Python 一起分发）。
  - 建议：用 `sqlite3.Row` 作 `row_factory` 取列名；大结果集分页/批量 `executemany`。

---

## 9) So what（最小体积 Win7 Python 桌面工作台：pin 版本）

- **运行时底座**：Windows 7 **SP1 x64**，预装 **KB2533623**（Python 3.8 必需）+ **VC++ 2015-2019 Redistributable（含 UCRT / KB2999226）**（编译轮子必需）。
- **Python**：**CPython 3.8.10 x64 完整安装器**（最后一个带二进制安装包的 3.8；勾选 `Include_tcltk=1`，即得 tkinter+IDLE）。**不要**用 embeddable 包（无 tkinter/pip）。**3.8 已 EOL（2024-10-07），无安全补丁 → 仅内网/受控环境使用，并锁死依赖版本。**
- **固定依赖（全部有 cp38/win_amd64 wheel）**：
  `numpy==1.24.4`、`pandas==2.0.3`（保守可用 1.5.3）、`openpyxl==3.1.5`、`python-docx==1.1.2`、`lxml==4.9.4`、`pywin32==306`（或 307/308 轮子）。**避开 pyarrow / parquet / Electron / Node / pnpm。**
- **UI**：首选 **tkinter（内置，零依赖、体积最小）**；要现代观感则 **Python `http.server`/Flask 本地 Web UI + Firefox ESR 115**（Win7 官方支持）。**Chrome 109 是 Win7 最后一版（2023-01 起不再安全更新）**，可作备选但不应作为长期宿主。
- **集成面**：
  - CATIA：`win32com.client.Dispatch("CATIA.Application")` 驱动**本机已授权的 CATIA V5（Win7 上用 R23–R28）**；仅做几何/参数驱动，铺层无 COM 自动化（见既有结论）。
  - Abaqus：以 `abaqus cae noGUI=xxx.py` / `abaqus python xxx.py` 调用，**脚本按内部 Python 2.6.2 语法**编写；结果经 ODB→CSV/JSON 回传。
  - 存储：**stdlib `sqlite3`**（文件型、零部署）。
  - 报告：**python-docx 1.1.2**（依赖 lxml 4.9.4）生成 .docx。

---

## 10) Confirmed vs Uncertain

**CONFIRMED（有官方/一手来源）**
- Python 3.8 是最后支持 Win7 的 CPython；3.9+ 安装器直接拒装（python.org 3.9.0 说明）。
- 3.8.10 是最后一个带二进制安装器的 3.8；3.8 EOL=2024-10-07。
- 3.8 完整安装器自带 tkinter/IDLE（`Include_tcltk` 默认 1）；embeddable 不含 tcl/tk；Win7 需 KB2533623。
- Node 12/13 支持 Win7（Tier 1），Node 14 起要求 Win8.1。
- .NET Framework 4.8 可装于 Win7 SP1；.NET Core 3.1、.NET 5、.NET 6 均支持 Win7 SP1；.NET 7+ 不支持；.NET 6 是最后支持者（2024-11-12 结束）。
- Abaqus 6.13-4 官方将 Win7 Professional SP1 列为 Qualified 平台；6.13 起仅 64-bit。
- numpy 1.24.4 / pandas 2.0.3 / openpyxl 3.1.5 / python-docx 1.1.2 / lxml 4.9.4 / pywin32 306–308 均支持 py3.8 且有 win_amd64 轮子。
- sqlite3 标准库在 Win7 无问题。
- Firefox ESR 115 官方支持 Win7；Edge 109（Chromium 109）为最后支持 Win7 的版本；Chrome 109 为最后支持 Win7 的 Chrome。

**UNCERTAIN（需现场/一手核对）**
- **CATIA R23–R28 支持 Win7** 的一手依据来自 DS Program Directory，本次仅取得二手整理（青腾表）。逐版精确支持级别（Qualified/Compatible）须登录 Program Directory 核对。
- **CATIA R22(V5-6R2012) 是否支持 Win7**：该二手表未覆盖，未确认。
- **PyQt5 5.15.11 在 Win7 实机运行**：版本对 py3.8 成立，但 Qt5.15 在 Win7 上的实机稳定性未逐一验证（推断可行）。
- **Abaqus 6.13-4 具体"已知 issue"清单**：官方 Release Notes 未取得逐条；现列注意点为通用工程经验。
- 若现场实际为 6.13 的其它 SP 或打了补丁，内部 Python 版本仍应以 `abaqus python -c "import sys;print(sys.version)"` 现场实测为准。
