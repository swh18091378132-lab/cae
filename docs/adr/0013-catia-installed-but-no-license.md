# 实测环境事实：CATIA V5-6R2022 已安装，但 COM 不可用（因许可证）

**Status: accepted（实测记录）**

## 实测结果（2026-10-23，目标机 DESKTOP-SRL8DH2）

### 已安装（CONFIRMED）

```
注册表 HKLM\SOFTWARE\Dassault Systemes\B32\0:
  DEST_FOLDER      = D:\Program Files\Dassault Systemes\B32
  DEST_FOLDER_OSDS = D:\Program Files\Dassault Systemes\B32\win_b64
  OSNAME           = WIN64
  TypeInstall      = V5-6R2022
环境文件: C:\ProgramData\DassaultSystemes\CATEnv\CATIA_P3.V5-6R2022.B32.txt
可执行:   ...\B32\win_b64\code\bin\CNEXT.exe / CATSTART.exe / CATIAENV.exe
```

- 版本 **V5-6R2022（B32）**，**64 位**（`win_b64`，无 `intel_a`）。
- 产品配置 **CATIA_P3**（P3 级配置）。

### 但 V5 Automation(COM) 不可用（CONFIRMED）

```
win32com.client.Dispatch('CATIA.Application')
  -> com_error (-2147221005, '无效的类字符串')
```

- 注册表 `HKLM\SOFTWARE\Classes\CATIA.Application`、`WOW6432Node`、`HKCU` **均不存在** → COM 类未注册。
- 用 `CATSTART.exe -env CATIA_P3.V5-6R2022.B32 -direnv ...` 启动后：
  - CNEXT 进程确实起来并持续运行；
  - 但主窗口标题为 **「许可证管理器」** → CATIA 卡在许可证对话框，未进入可自动化的会话；
  - COM 依旧 `GetActiveObject`/`Dispatch` 均失败。

### 结论

CATIA V5-6R2022 **软件本体已装**，但：

1. **COM 接口未注册**——通常需在 CATIA 正常启动一次并成功获取许可证后由 CATIA 自行注册，或经 CATIAENV/安装后配置步骤完成；
2. **当前无可用 CATIA 许可证**——这是根因。许可证管理器弹窗说明本机（或所连许可服务器）未提供 CATIA 授权。

## 对平台的影响

- `RealCatiaDriver`（V5 Automation 路径）**当前无法在本机验证**；代码已实现（`core/catia_driver.py`），待现场解决许可证/COM 注册后再验。
- **P1 的其余环节不受影响**：Abaqus 2023 真机链路已跑通（求解 + ODB 抽取 + 校核 + 报告），铺层作 CAE 属性（ADR-0005）本就绕开了 CATIA 复材模块。
- fake 适配器保证开发与回归可继续，UI 默认即 fake 模式。

## 现场需完成的两步（只有人能操作）

1. **解决 CATIA 许可证**：确认许可服务器（DSLS）地址与授权是否覆盖 CATIA V5-6R2022 P3 配置。参考 `C:\Program Files (x86)\Dassault Systemes\DS License Server`。
2. **让 CATIA 正常启动一次**（手动双击、通过许可证对话框完成认证），使其注册 COM 类；之后用以下命令验证：
   ```
   python -c "import win32com.client; print(win32com.client.Dispatch('CATIA.Application'))"
   ```
   成功后再跑 `python main.py run wingbox_v1 --real`。

## 附带事实：pywin32

本机开发环境原先**未安装 pywin32**（`No module named 'win32com'`），已安装并以 `import win32com.client` 验证通过。目标机部署时需按 `requirements.txt` 安装 `pywin32==306`（Win7 + py3.8）。
