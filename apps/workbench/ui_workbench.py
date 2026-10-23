# -*- coding: utf-8 -*-
"""
ui_workbench.py -- 本地工程工作台 UI（票 08）

Tkinter 最小实现（Win7 无浏览器依赖）。**只调用 workbench_service**，
自身不含编排逻辑，便于把可测逻辑留在服务层。

布局：
  左：模板选择 + 参数输入 + 运行按钮
  右上：运行日志
  右下：血缘树 + 报告按钮

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


class WorkbenchApp(object):
    def __init__(self, root, service):
        self.root = root
        self.svc = service
        self.current_study = None
        self._queue = []
        self._running = False

        root.title("CAE 结构设计分析工作台 — P1")
        root.geometry("1100x720")

        self._build_ui()
        self._poll_queue()

    # ---------------- UI 构建 ----------------

    def _build_ui(self):
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
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

        self._load_templates()
        self._refresh_studies()

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
