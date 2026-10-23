# -*- coding: utf-8 -*-
"""
ui_workbench.py -- 本地工程工作台 UI（票 08 / 票 19）

Tkinter 最小实现（Win7 无浏览器依赖）。**只调用 workbench_service**，
自身不含编排逻辑，便于把可测逻辑留在服务层。

布局（票19 起分两个标签页，原有布局与功能保持不变）：
  「分析」页
    左：模板选择 + 参数输入 + 运行按钮 + 已有分析任务
    右上：运行日志
    右下：血缘树 + 报告按钮
  「审批与型号汇总」页（票19 新增）
    上：角色切换（审核 / 签发 / 设计 / 强度分析）+ 刷新
    左：待办列表（按角色区分：待审 / 待签 / 已退回）+ 型号汇总视图
    右：审核详情（血缘 + 校核明细 + 审批历史）+ 通过 / 退回 / 签发
    签发按钮**只对已通过审核的报告可用**（不满足时置灰并在提示里说明原因）。

兼容 Python 3.8。
"""

import os
import sys
import json
import threading

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError:  # pragma: no cover
    tk = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from workbench_service import WorkbenchService, ServiceError
import workbench_service as _ws


# 审批页的角色下拉项：（角色键, 显示名）
ROLE_CHOICES = (
    ("reviewer", "审核（待审清单）"),
    ("signer", "签发（待签清单）"),
    ("design", "设计（已退回）"),
    ("analysis", "强度分析（已退回）"),
)


class WorkbenchApp(object):
    def __init__(self, root, service):
        self.root = root
        self.svc = service
        self.current_study = None
        self._queue = []
        self._running = False

        # 审批页状态（票19）
        self.role_var = None
        self._inbox_rows = []
        self._review_detail = None
        self._model_ids = []

        root.title("CAE 结构设计分析工作台 — P2")
        root.geometry("1180x760")

        self._build_ui()
        self._poll_queue()

    # ---------------- UI 构建 ----------------

    def _build_ui(self):
        """顶层 Notebook：保留原「分析」页，新增「审批与型号汇总」页。"""
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.analysis_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.analysis_tab, text="分析")
        self._build_analysis_tab(self.analysis_tab)

        self.approval_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.approval_tab, text="审批与型号汇总")
        self._build_approval_tab(self.approval_tab)

        self._load_templates()
        self._refresh_studies()
        self._refresh_models()
        self.on_refresh_inbox()
        self.on_refresh_rollup()

    def _build_analysis_tab(self, parent):
        main = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # 左：参数区
        left = ttk.Frame(main, width=340)
        main.add(left, weight=0)

        ttk.Label(left, text="1. 选择结构族模板").pack(anchor="w", pady=(4, 2))
        self.tpl_var = tk.StringVar()
        self.tpl_combo = ttk.Combobox(left, textvariable=self.tpl_var,
                                      state="readonly", width=34)
        self.tpl_combo.pack(anchor="w", fill=tk.X)
        self.tpl_combo.bind("<<ComboboxSelected>>", lambda e: self._on_template())

        ttk.Label(left, text="2. 参数（参数契约内的具名参数）").pack(
            anchor="w", pady=(10, 2))
        self.param_frame = ttk.Frame(left)
        self.param_frame.pack(anchor="w", fill=tk.X)
        self.param_vars = {}

        ttk.Label(left, text="3. 校核准则").pack(anchor="w", pady=(10, 2))
        crit_row = ttk.Frame(left)
        crit_row.pack(anchor="w", fill=tk.X)
        self.crit_tsai = tk.BooleanVar(value=True)
        self.crit_hashin = tk.BooleanVar(value=True)
        ttk.Checkbutton(crit_row, text="Tsai-Wu", variable=self.crit_tsai).pack(side=tk.LEFT)
        ttk.Checkbutton(crit_row, text="Hashin", variable=self.crit_hashin).pack(side=tk.LEFT)

        ttk.Label(left, text="4. 适配器模式").pack(anchor="w", pady=(10, 2))
        self.mode_var = tk.StringVar(value=self.svc.mode)
        mode_row = ttk.Frame(left)
        mode_row.pack(anchor="w", fill=tk.X)
        ttk.Radiobutton(mode_row, text="演示(fake，无需 CATIA/Abaqus)",
                        variable=self.mode_var, value="fake").pack(anchor="w")
        ttk.Radiobutton(mode_row, text="真机(real)",
                        variable=self.mode_var, value="real").pack(anchor="w")

        self.run_btn = ttk.Button(left, text="▶ 运行完整链（建模→求解→校核→报告）",
                                  command=self.on_run)
        self.run_btn.pack(anchor="w", pady=14, fill=tk.X)

        ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)

        ttk.Label(left, text="已有分析任务").pack(anchor="w")
        self.study_list = tk.Listbox(left, height=8)
        self.study_list.pack(anchor="w", fill=tk.BOTH, expand=True)
        self.study_list.bind("<<ListboxSelect>>", lambda e: self._on_study_select())

        # 右：日志 + 血缘
        right = ttk.PanedWindow(main, orient=tk.VERTICAL)
        main.add(right, weight=1)

        log_frame = ttk.LabelFrame(right, text="运行日志")
        right.add(log_frame, weight=1)
        self.log = tk.Text(log_frame, height=14, wrap="word")
        self.log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        lin_frame = ttk.LabelFrame(right, text="血缘（可追溯性）")
        right.add(lin_frame, weight=1)

        btn_row = ttk.Frame(lin_frame)
        btn_row.pack(fill=tk.X, padx=4, pady=(4, 0))
        ttk.Button(btn_row, text="打开报告", command=self.on_open_report).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="打开报告目录", command=self.on_open_dir).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_row, text="刷新血缘", command=self.on_refresh_lineage).pack(side=tk.LEFT)

        self.lineage = tk.Text(lin_frame, height=14, wrap="none")
        self.lineage.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ---------------- 审批与型号汇总页（票19） ----------------

    def _build_approval_tab(self, parent):
        """审批工作台：角色切换 + 待办列表 + 审核详情 + 通过/退回/签发 + 型号汇总。"""
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, padx=6, pady=(6, 2))
        ttk.Label(top, text="当前角色").pack(side=tk.LEFT)
        self.role_var = tk.StringVar(value=ROLE_CHOICES[0][1])
        self.role_combo = ttk.Combobox(
            top, textvariable=self.role_var, state="readonly", width=24,
            values=[c[1] for c in ROLE_CHOICES])
        self.role_combo.pack(side=tk.LEFT, padx=6)
        self.role_combo.bind("<<ComboboxSelected>>",
                             lambda e: self.on_role_change())
        ttk.Button(top, text="刷新待办", command=self.on_refresh_inbox).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(top, text="刷新型号汇总",
                   command=self.on_refresh_rollup).pack(side=tk.LEFT)

        body = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # 左：待办 + 型号汇总
        left = ttk.PanedWindow(body, orient=tk.VERTICAL)
        body.add(left, weight=1)

        inbox_frame = ttk.LabelFrame(left, text="待办（按角色区分）")
        left.add(inbox_frame, weight=1)
        self.inbox = tk.Listbox(inbox_frame, height=10)
        self.inbox.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.inbox.bind("<<ListboxSelect>>", lambda e: self.on_inbox_select())

        rollup_frame = ttk.LabelFrame(left, text="型号汇总：各部件报告流程状态")
        left.add(rollup_frame, weight=1)
        rrow = ttk.Frame(rollup_frame)
        rrow.pack(fill=tk.X, padx=4, pady=(4, 0))
        ttk.Label(rrow, text="型号配置").pack(side=tk.LEFT)
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(rrow, textvariable=self.model_var,
                                       state="readonly", width=34)
        self.model_combo.pack(side=tk.LEFT, padx=6)
        self.model_combo.bind("<<ComboboxSelected>>",
                              lambda e: self.on_refresh_rollup())
        self.rollup_text = tk.Text(rollup_frame, height=10, wrap="none")
        self.rollup_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 右：审核详情 + 动作
        right = ttk.LabelFrame(body, text="审核详情（血缘 + 校核明细 + 审批历史）")
        body.add(right, weight=2)

        act = ttk.Frame(right)
        act.pack(fill=tk.X, padx=4, pady=(4, 0))
        self.btn_approve = ttk.Button(act, text="审核通过…",
                                      command=self.on_approve)
        self.btn_approve.pack(side=tk.LEFT)
        self.btn_reject = ttk.Button(act, text="审核退回…",
                                     command=self.on_reject)
        self.btn_reject.pack(side=tk.LEFT, padx=6)
        self.btn_sign = ttk.Button(act, text="签发", command=self.on_sign)
        self.btn_sign.pack(side=tk.LEFT, padx=(18, 0))
        self.btn_open_rep = ttk.Button(act, text="打开报告",
                                       command=self.on_open_review_report)
        self.btn_open_rep.pack(side=tk.LEFT, padx=6)

        self.detail = tk.Text(right, height=26, wrap="none")
        self.detail.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._update_action_buttons()

    # ---------------- 审批页数据加载（票19） ----------------

    def _role_key(self):
        """下拉框显示名 -> 角色键（reviewer / signer / design / analysis）。"""
        text = self.role_var.get() if self.role_var is not None else ""
        for key, name in ROLE_CHOICES:
            if name == text:
                return key
        return ROLE_CHOICES[0][0]

    def on_role_change(self):
        self.on_refresh_inbox()

    def on_refresh_inbox(self):
        """刷新当前角色的待办清单（所有数据来自服务层，UI 不做判断）。"""
        role = self._role_key()
        self.inbox.delete(0, tk.END)
        self._inbox_rows = []
        try:
            rows = self.svc.approval_inbox(role)
        except Exception as exc:
            self._log("加载待办失败: %s" % exc)
            rows = []
        for row in rows:
            self._inbox_rows.append(row)
            self.inbox.insert(tk.END, row.get("label") or row.get("study_id"))
        if rows:
            self.inbox.selection_set(0)
            self.on_inbox_select()
        else:
            self._review_detail = None
            self.detail.delete("1.0", tk.END)
            self.detail.insert(tk.END, "当前角色（%s）没有待处理事项。"
                               % _ws.approval.role_name(role))
            self._update_action_buttons()

    def on_inbox_select(self):
        sel = self.inbox.curselection()
        if not sel or sel[0] >= len(self._inbox_rows):
            return
        row = self._inbox_rows[sel[0]]
        try:
            # 传 role：服务层只回该角色**可见**的动作（UI 不复制角色权限表）
            detail = self.svc.get_review_detail(row["study_id"],
                                                row["version"],
                                                role=self._role_key())
        except ServiceError as exc:
            self._review_detail = None
            self.detail.delete("1.0", tk.END)
            self.detail.insert(tk.END, str(exc))
            self._update_action_buttons()
            return
        self._review_detail = detail
        self.detail.delete("1.0", tk.END)
        self.detail.insert(tk.END, detail.get("text") or "")
        self.current_study = row["study_id"]
        self._update_action_buttons()

    def _update_action_buttons(self):
        """
        按"报告状态 + 当前角色"决定按钮可用性。

        关键验收：签发按钮**只对已通过审核的报告可用**；
        未通过审核时置灰（服务层 can_sign_report 给出可读原因，这里只消费）。
        """
        detail = self._review_detail
        actions = (detail or {}).get("actions") or []
        self.btn_approve.config(
            state=(tk.NORMAL if _ws.APPROVAL_ACTION_APPROVE in actions
                   else tk.DISABLED))
        self.btn_reject.config(
            state=(tk.NORMAL if _ws.APPROVAL_ACTION_REJECT in actions
                   else tk.DISABLED))
        can_sign = bool((detail or {}).get("can_sign")) and \
            _ws.APPROVAL_ACTION_SIGN in actions
        self.btn_sign.config(state=(tk.NORMAL if can_sign else tk.DISABLED))

    # ---------------- 审批动作（票19） ----------------

    def _ask_text(self, title, prompt, initial=""):
        """简易多行意见输入框（退回必须填意见，所以不用 simpledialog）。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        ttk.Label(win, text=prompt).pack(anchor="w", padx=8, pady=(8, 2))
        txt = tk.Text(win, height=5, width=60)
        txt.insert("1.0", initial)
        txt.pack(padx=8, pady=4)
        result = {"text": None}

        def _ok():
            result["text"] = txt.get("1.0", tk.END).strip()
            win.destroy()

        def _cancel():
            win.destroy()

        row = ttk.Frame(win)
        row.pack(pady=(0, 8))
        ttk.Button(row, text="确定", command=_ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="取消", command=_cancel).pack(side=tk.LEFT)
        try:
            self.root.wait_window(win)
        except Exception:
            pass
        return result["text"]

    def on_approve(self):
        if not self._review_detail:
            return
        d = self._review_detail
        opinion = self._ask_text("审核通过", "审核意见（可选）：")
        if opinion is None:
            return
        try:
            self.svc.approve_report(d["study_id"], opinion=opinion or "同意",
                                    version=d["version"])
        except ServiceError as exc:
            messagebox.showerror("审核失败", str(exc))
            return
        self._log("审核通过：%s 报告 v%s" % (d["study_id"], d["version"]))
        self.on_refresh_inbox()
        self.on_refresh_rollup()

    def on_reject(self):
        if not self._review_detail:
            return
        d = self._review_detail
        opinion = self._ask_text("审核退回", "退回意见（必填，说明问题与修改要求）：")
        if opinion is None:
            return
        if not opinion:
            messagebox.showwarning("提示", "退回必须填写意见")
            return
        try:
            self.svc.reject_report(d["study_id"], opinion=opinion,
                                   version=d["version"])
        except ServiceError as exc:
            messagebox.showerror("退回失败", str(exc))
            return
        self._log("审核退回：%s 报告 v%s" % (d["study_id"], d["version"]))
        self.on_refresh_inbox()
        self.on_refresh_rollup()

    def on_sign(self):
        """签发：UI 只是消费服务层的判定结果（未通过审核时按钮本就置灰）。"""
        if not self._review_detail:
            return
        d = self._review_detail
        try:
            self.svc.assert_can_sign(d["study_id"], d["version"])
        except ServiceError as exc:
            messagebox.showwarning("不能签发", str(exc))
            return
        opinion = self._ask_text("签发", "签发意见（可选）：")
        if opinion is None:
            return
        try:
            self.svc.sign_report(d["study_id"], opinion=opinion or None,
                                 version=d["version"])
        except ServiceError as exc:
            messagebox.showerror("签发失败", str(exc))
            return
        self._log("已签发：%s 报告 v%s（最终版）"
                  % (d["study_id"], d["version"]))
        self.on_refresh_inbox()
        self.on_refresh_rollup()

    def on_open_review_report(self):
        if not self._review_detail:
            return
        p = self._review_detail.get("report_path")
        if not p:
            messagebox.showinfo("提示", "该报告没有可打开的文件")
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(p)  # noqa
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    # ---------------- 型号汇总视图（票19） ----------------

    def _refresh_models(self):
        """型号配置下拉框（供汇总视图选择）。"""
        try:
            models = self.svc.list_models()
        except Exception as exc:
            self._log("加载型号配置失败: %s" % exc)
            models = []
        self._model_ids = list(models)
        labels = ["%s  %s" % (m.get("model_id"), m.get("name") or "")
                  for m in models]
        self.model_combo["values"] = labels
        if labels:
            self.model_combo.current(0)
        else:
            self.model_var.set("")

    def _selected_model_id(self):
        idx = self.model_combo.current()
        if idx < 0 or idx >= len(self._model_ids):
            return None
        return self._model_ids[idx].get("model_id")

    def on_refresh_rollup(self):
        """刷新型号汇总视图：各部件报告处于哪个流程环节。"""
        self.rollup_text.delete("1.0", tk.END)
        if not self._model_ids:          # 型号配置是别的入口建的：这里顺带重载一次
            self._refresh_models()
        mid = self._selected_model_id()
        if not mid:
            self.rollup_text.insert(tk.END, "（尚无型号配置）")
            return
        try:
            overview = self.svc.get_model_approval_overview(mid)
        except ServiceError as exc:
            self.rollup_text.insert(tk.END, "加载型号汇总失败: %s" % exc)
            return
        self.rollup_text.insert(tk.END, overview.get("text") or "")


    # ---------------- 数据加载 ----------------

    def _load_templates(self):
        try:
            tpls = self.svc.list_templates()
        except Exception as exc:
            self._log("加载模板失败: %s" % exc)
            tpls = []
        self.tpl_combo["values"] = tpls
        if tpls:
            self.tpl_combo.current(0)
            self._on_template()

    def _on_template(self):
        for w in self.param_frame.winfo_children():
            w.destroy()
        self.param_vars = {}
        tid = self.tpl_var.get()
        if not tid:
            return
        try:
            tpl = self.svc.get_template(tid)
        except Exception as exc:
            self._log("读取模板失败: %s" % exc)
            return
        for spec in tpl.parameters:
            row = ttk.Frame(self.param_frame)
            row.pack(anchor="w", fill=tk.X, pady=1)
            ttk.Label(row, text=spec["name"], width=14).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(spec.get("default", "")))
            self.param_vars[spec["name"]] = (var, spec)
            ttk.Entry(row, textvariable=var, width=14).pack(side=tk.LEFT)

    def _refresh_studies(self):
        self.study_list.delete(0, tk.END)
        try:
            for s in self.svc.list_studies():
                self.study_list.insert(
                    tk.END, "%s  (%d 产物)" % (s["id"], s["n_artifacts"]))
                self.study_list.itemconfig(tk.END, foreground="black")
        except Exception as exc:
            self._log("加载 Study 列表失败: %s" % exc)

    def _on_study_select(self):
        sel = self.study_list.curselection()
        if not sel:
            return
        text = self.study_list.get(sel[0])
        sid = text.split("  (")[0].strip()
        self.current_study = sid
        self.on_refresh_lineage()

    # ---------------- 运行 ----------------

    def _collect_params(self):
        params = {}
        for name, (var, spec) in self.param_vars.items():
            raw = var.get().strip()
            if raw == "":
                continue
            if spec.get("type") == "int":
                params[name] = int(raw)
            elif spec.get("type") == "float":
                params[name] = float(raw)
            else:
                params[name] = raw
        return params

    def on_run(self):
        if self._running:
            return
        tid = self.tpl_var.get()
        if not tid:
            messagebox.showwarning("提示", "请先选择模板")
            return
        try:
            params = self._collect_params()
        except ValueError as exc:
            messagebox.showerror("参数错误", "参数格式有误: %s" % exc)
            return

        crits = []
        if self.crit_tsai.get():
            crits.append("tsai_wu")
        if self.crit_hashin.get():
            crits.append("hashin")
        if not crits:
            messagebox.showwarning("提示", "请至少选择一种校核准则")
            return

        self._running = True
        self.run_btn.config(state=tk.DISABLED)
        self._log("=" * 60)
        self._log("开始运行：模板=%s  模式=%s" % (tid, self.mode_var.get()))
        self._log("参数: %s" % json.dumps(params, ensure_ascii=False, sort_keys=True))

        thread = threading.Thread(target=self._run_worker,
                                  args=(tid, params, crits), daemon=True)
        thread.start()

    def _run_worker(self, tid, params, crits):
        """后台线程：只做计算，结果通过队列回主线程。"""
        try:
            self.svc.mode = self.mode_var.get()
            sid = self.svc.start_study("工作台分析 %s" % tid)
            self._queue.append(("log", "已创建 Study: %s" % sid))
            res = self.svc.run_chain(
                sid, params, tid, rules={"criteria": crits})
            self._queue.append(("done", (sid, res)))
        except ServiceError as exc:
            self._queue.append(("error", str(exc)))
        except Exception as exc:
            self._queue.append(("error", "%s: %s" % (type(exc).__name__, exc)))

    def _poll_queue(self):
        while self._queue:
            kind, payload = self._queue.pop(0)
            if kind == "log":
                self._log(payload)
            elif kind == "done":
                sid, res = payload
                stdout = self._summarize(res)
                self._log(stdout)
                chk = res.get("check") or {}
                rep = res.get("report") or {}
                if rep.get("path"):
                    self._log("报告: %s" % rep["path"])
                if rep.get("warnings"):
                    for w in rep["warnings"]:
                        self._log("  [warn] %s" % w)
                if chk.get("summary"):
                    self._log("校核结论: %s" % json.dumps(
                        chk["summary"], ensure_ascii=False, sort_keys=True))
                self.current_study = sid
                self._running = False
                self.run_btn.config(state=tk.NORMAL)
                self._refresh_studies()
                self.on_refresh_lineage()
                # 新报告产出即登记为待审核（票18），审批页随即能看到它（票19）
                try:
                    self.on_refresh_inbox()
                    self.on_refresh_rollup()
                except Exception as exc:
                    self._log("审批页刷新失败: %s" % exc)
            elif kind == "error":
                self._log("[错误] %s" % payload)
                messagebox.showerror("运行失败", payload)
                self._running = False
                self.run_btn.config(state=tk.NORMAL)
        self.root.after(120, self._poll_queue)

    def _summarize(self, res):
        lines = ["链完成。产物："]
        arts = res.get("artifacts") or {}
        for kind in sorted(arts.keys()):
            lines.append("  %-14s #%s" % (kind, arts[kind]))
        return "\n".join(lines)

    # ---------------- 报告 / 血缘 ----------------

    def on_refresh_lineage(self):
        if not self.current_study:
            return
        try:
            self.lineage.delete("1.0", tk.END)
            self.lineage.insert(tk.END,
                                self.svc.format_lineage_text(self.current_study))
        except Exception as exc:
            self._log("血缘刷新失败: %s" % exc)

    def on_open_report(self):
        if not self.current_study:
            messagebox.showinfo("提示", "请先选择一个分析任务")
            return
        try:
            p = self.svc.open_report(self.current_study)
            self._log("已打开报告: %s" % p)
        except ServiceError as exc:
            messagebox.showinfo("提示", str(exc))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def on_open_dir(self):
        if not self.current_study:
            return
        p = self.svc.get_report_path(self.current_study)
        if not p:
            messagebox.showinfo("提示", "该 Study 尚无报告")
            return
        d = os.path.dirname(p)
        try:
            if hasattr(os, "startfile"):
                os.startfile(d)  # noqa
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _log(self, text):
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)


def launch(work_root=None, mode="fake", auto_close_ms=None):
    """
    启动 UI。auto_close_ms 仅供冒烟测试（自动关闭窗口）。
    返回退出码。
    """
    if tk is None:
        raise RuntimeError("Tkinter 不可用")

    work_root = work_root or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "data", "work")
    svc = WorkbenchService(work_root, mode=mode)
    root = tk.Tk()
    app = WorkbenchApp(root, svc)
    if auto_close_ms:
        root.after(int(auto_close_ms), root.destroy)
    try:
        root.mainloop()
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(launch())
