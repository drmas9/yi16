# -*- coding: utf-8 -*-
"""
浙江大学心理与行为科学系通知监控窗口
====================================

这个程序是「心理系爬虫.py」的图形界面（GUI）版本，界面样式、配色和交互
与「爬虫监控窗口.py」完全一致（深蓝主题 + 卡片列表），区别只在于：
本窗口只监控心理与行为科学系这一个信息源，重点跟踪「转专业」通知。

主要功能：
- 深蓝色主题的卡片式界面（与 爬虫监控窗口.py 同款）
- 可按分类筛选浏览通知（转专业 / 推免 / 毕业论文 / 科研训练 / ……）
- 「转专业」类通知始终排在最前面，且用最醒目的红色胶囊标记
- 后台线程定时抓取，发现新通知时实时提醒（弹窗 + 标题栏标记 + 列表高亮）
- 点击卡片上的「打开」按钮，用系统浏览器访问原文

一个与大监控窗口不同的小改进：
    首次启动抓到的全部通知会作为「基线」记入已见文件，不会被误报成新消息；
    只有启动之后新发布的通知才会弹窗提醒。

运行方式：
    python 心理系监控窗口.py

依赖：仅使用 Python 标准库（tkinter），无需安装第三方包。
"""

import json          # 读写 JSON 文件（保存"已经看过的通知链接"）
import os            # 拼接文件路径
import sys           # 把当前目录加入模块搜索路径，便于导入同目录爬虫
import threading     # 后台线程（抓取时不卡住界面）
import time          # 休眠等待（轮询间隔）
import tkinter as tk # Python 自带的图形界面库，tk 是常用别名
from datetime import datetime      # 记录抓取时间
from tkinter import messagebox, ttk  # messagebox：弹窗；ttk：带样式的滚动条

# 把本文件所在目录加入 Python 模块搜索路径，才能导入同目录的爬虫模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import 心理系爬虫 as psych_crawler  # noqa: E402  # 导入心理系爬虫模块

# ===========================================================================
# 配色方案（与 爬虫监控窗口.py 完全一致的深蓝主题）
# ===========================================================================
BG_DARK = "#0F2B5B"        # 窗口最外层背景：深蓝
BG_CARD = "#1A3A6B"        # 卡片背景：稍亮一点的蓝色
BG_INPUT = "#FFFFFF"       # 输入框背景：白色
TEXT_PRIMARY = "#FFFFFF"   # 主要文字颜色：白色
TEXT_SECOND = "#8BA4CC"    # 次要文字颜色：浅蓝灰
ACCENT_BLUE = "#4A90FF"    # 强调色：蓝色
ACCENT_GREEN = "#34D399"   # 强调色：绿色（"立即刷新"按钮）
ACCENT_RED = "#F87171"     # 强调色：红色（"新消息"标记、转专业分类）
ACCENT_YELLOW = "#FBBF24"  # 强调色：黄色
ACCENT_ORANGE = "#FB923C"  # 强调色：橙色
BORDER_COLOR = "#2A4A7A"   # 分隔线颜色

# 心理系通知分类对应的颜色（显示在卡片上的小胶囊标签）。
# 「转专业」是本窗口最关注的分类，特意使用最醒目的红色。
CATEGORY_COLORS = {
    "转专业": ACCENT_RED,
    "推免": "#A78BFA",
    "毕业论文": ACCENT_BLUE,
    "科研训练": ACCENT_GREEN,
    "国际交流": "#22D3EE",
    "教务通知": ACCENT_YELLOW,
    "其他": "#6B7280",
}

WINDOW_TITLE = "心理与行为科学系通知监控"

# 后台轮询间隔（秒）：默认 30 分钟
DEFAULT_INTERVAL = 30 * 60

# 本窗口专用的"已见链接"文件。
# 注意：刻意不与命令行爬虫的 psych_seen.json 共用，避免两种使用方式互相干扰。
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(DATA_DIR, "psych_gui_seen.json")


# ===========================================================================
# UI 组件（圆角按钮，与 爬虫监控窗口.py 同款）
# ===========================================================================
class RoundedButton(tk.Canvas):
    """圆角按钮 —— 用 Canvas 画布手工绘制，带鼠标悬停(hover)变色效果。

    tk.Button 不支持圆角样式，所以用"左右两个半圆 + 中间一个矩形"
    拼出圆角矩形外观。
    """

    def __init__(self, master, text, command=None, width=80, height=32,
                 bg=ACCENT_BLUE, fg="white", hover_color=None,
                 font=("微软雅黑", 10), bg_parent=BG_DARK, **kwargs):
        super().__init__(master, width=width, height=height,
                         bg=bg_parent, highlightthickness=0, **kwargs)
        self.command = command
        self.text = text
        self.bg = bg
        self.fg = fg
        self.hover_color = hover_color or self._lighten(bg, 25)
        self.font = font
        self.radius = height // 2
        self.width = width
        self.height = height
        self._draw_button(bg)
        self.bind("<Button-1>", self._on_click)   # 左键点击
        self.bind("<Enter>", self._on_enter)      # 鼠标进入
        self.bind("<Leave>", self._on_leave)      # 鼠标离开

    def _draw_button(self, color):
        """两个圆 + 一个矩形 = 圆角矩形，再居中写文字。"""
        self.delete("all")
        r = self.radius
        w, h = self.width, self.height
        self.create_oval(0, 0, 2 * r, 2 * r, fill=color, outline=color)
        self.create_oval(w - 2 * r, 0, w, 2 * r, fill=color, outline=color)
        self.create_rectangle(r, 0, w - r, h, fill=color, outline=color)
        self.create_text(w / 2, h / 2, text=self.text, fill=self.fg, font=self.font)

    def _on_enter(self, e):
        self._draw_button(self.hover_color)

    def _on_leave(self, e):
        self._draw_button(self.bg)

    def _on_click(self, e):
        if self.command:
            self.command()

    def _lighten(self, hex_color, amount):
        """把十六进制颜色调亮 amount 个单位。"""
        hex_color = hex_color.lstrip("#")
        r, g, b = [int(hex_color[i:i + 2], 16) for i in (0, 2, 4)]
        r = min(255, r + amount)
        g = min(255, g + amount)
        b = min(255, b + amount)
        return f"#{r:02x}{g:02x}{b:02x}"


class CategoryChip(tk.Canvas):
    """分类标签胶囊：卡片上带颜色的小标签，标识通知属于哪个分类。"""

    def __init__(self, master, text, color="#6B7280", width=None, height=24,
                 bg_parent=BG_DARK, **kwargs):
        self.text_str = text
        if width is None:
            width = max(40, len(text) * 12 + 18)   # 按文字数估算宽度
        super().__init__(master, width=width, height=height,
                         bg=bg_parent, highlightthickness=0, **kwargs)
        self.color = color
        self.width = width
        self.height = height
        self.radius = height // 2
        self._draw()

    def _draw(self):
        self.delete("all")
        r = self.radius
        w, h = self.width, self.height
        self.create_oval(0, 0, 2 * r, 2 * r, fill=self.color, outline=self.color)
        self.create_oval(w - 2 * r, 0, w, 2 * r, fill=self.color, outline=self.color)
        self.create_rectangle(r, 0, w - r, h, fill=self.color, outline=self.color)
        self.create_text(w / 2, h / 2, text=self.text_str,
                         fill="white", font=("微软雅黑", 8, "bold"))


# ===========================================================================
# 主窗口
# ===========================================================================
class PsychMonitorApp:
    """监控窗口主类：界面布局、后台抓取、轮询与新消息提醒。"""

    def __init__(self, root):
        self.root = root
        self.root.geometry("860x640")
        self.root.configure(bg=BG_DARK)
        self.root.title(WINDOW_TITLE)

        # 数据状态
        self.all_news = []          # 抓到的全部通知
        self.new_hrefs = set()      # 本次运行中标记为"新消息"的链接
        self.category = "全部"      # 当前选中的分类筛选
        self.list_frame = None     # 卡片容器（build_main 中创建）

        # 轮询状态
        self.interval_sec = DEFAULT_INTERVAL
        self.running = True
        self.worker = None
        self.last_check_time = None   # 最近一次成功抓取的时间
        self.baseline_done = False    # 是否已建立"已见链接"基线

        # 先构建界面（立即显示"正在抓取"），再启动后台线程抓取，
        # 避免启动时界面长时间无响应。
        self.build_main()
        self._start_worker()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ 数据
    def _load_seen(self):
        """读取"已看过的通知链接"集合；文件不存在或损坏时返回空集合。"""
        if os.path.exists(SEEN_FILE):
            try:
                return set(json.loads(open(SEEN_FILE, encoding="utf-8").read())
                           .get("hrefs", []))
            except Exception:
                return set()
        return set()

    def _save_seen(self, hrefs):
        """把"已看过的通知链接"保存到本地 JSON。"""
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"hrefs": sorted(hrefs),
                       "updated": datetime.now().isoformat(timespec="seconds")},
                      f, ensure_ascii=False, indent=2)

    def _do_fetch(self):
        """抓取心理系通知（含正文摘要），成功返回 True，失败返回 False。

        抓取过程含多个网络请求（约 27 个），耗时数秒，
        所以始终在后台线程中调用本方法。
        """
        try:
            # log 传一个空函数：抓取过程的进度信息不打扰图形界面
            items = psych_crawler.scrape(
                pages=psych_crawler.DEFAULT_PAGES,
                with_content=True,
                log=lambda *a, **k: None,
            )
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("抓取失败", str(e)))
            return False
        # 统一补上 meta 字段（心理系通知没有"回复/浏览"这类附加信息，留空）
        for n in items:
            n["meta"] = ""
        self.all_news = items
        return True

    # ------------------------------------------------------------------ UI
    def build_main(self):
        """构建整个界面。切换分类时先清空再重建（与大监控窗口做法一致）。"""
        for w in self.root.winfo_children():
            w.destroy()

        # 顶部标题区：标题（左）+ 抓取状态（右）
        top = tk.Frame(self.root, bg=BG_DARK)
        top.pack(fill="x", padx=25, pady=(18, 10))

        # 保留标题 Label 的引用：轮询拿到新数据后只改文字，不重建整个界面
        self.header_label = tk.Label(top, text=self._title_text(),
                                     font=("微软雅黑", 15, "bold"),
                                     bg=BG_DARK, fg=TEXT_PRIMARY)
        self.header_label.pack(side="left")

        self.status_label = tk.Label(top, text=self._status_text(),
                                     font=("微软雅黑", 9),
                                     bg=BG_DARK, fg=TEXT_SECOND)
        self.status_label.pack(side="right")

        # 分类筛选条："全部" + 爬虫模块定义好的分类顺序
        cat_bar = tk.Frame(self.root, bg=BG_DARK)
        cat_bar.pack(fill="x", padx=25, pady=(0, 8))
        for cat in ["全部"] + psych_crawler.CATEGORY_ORDER:
            is_active = (cat == self.category)
            btn = tk.Label(
                cat_bar, text=cat,
                font=("微软雅黑", 9, "bold") if is_active else ("微软雅黑", 9),
                bg=ACCENT_BLUE if is_active else BG_CARD,
                fg="white", padx=10, pady=5, cursor="hand2",
            )
            btn.pack(side="left", padx=(0, 6))
            # c=cat 绑定当前分类名，避免 lambda 闭包陷阱
            btn.bind("<Button-1>", lambda e, c=cat: self.select_category(c))

        # 分隔线
        tk.Frame(self.root, height=1, bg=BORDER_COLOR).pack(fill="x", padx=25, pady=2)

        # 滚动列表：Canvas + 滚动条 + 内部 Frame
        list_container = tk.Frame(self.root, bg=BG_DARK)
        list_container.pack(side="top", fill="both", expand=True, padx=25, pady=4)

        canvas = tk.Canvas(list_container, bg=BG_DARK, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical",
                                  command=canvas.yview)
        self.list_frame = tk.Frame(canvas, bg=BG_DARK)

        self.list_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw",
                             tags=("list_window",))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig("list_window", width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 鼠标滚轮滚动
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # 底部操作区（固定高度）
        bottom = tk.Frame(self.root, bg=BG_DARK, height=70)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        self._build_bottom(bottom)

        self._render_list()

    def _title_text(self):
        """顶部标题，如"心理与行为科学系通知监控（51 条）"。"""
        return f"{WINDOW_TITLE}（{len(self.all_news)} 条）"

    def _status_text(self):
        """状态栏文字：上次检查时间 + 轮询间隔。"""
        t = self.last_check_time
        ts = t.strftime("%H:%M:%S") if t else "—"
        return f"上次检查：{ts}    间隔：{self.interval_sec // 60} 分钟"

    def _build_bottom(self, parent):
        """底部：立即刷新、轮询间隔设置、清空新消息标记。"""
        RoundedButton(parent, "立即刷新", command=self.manual_refresh,
                      width=100, height=34, bg=ACCENT_GREEN,
                      bg_parent=BG_DARK).pack(side="left", padx=25, pady=18)

        tk.Label(parent, text="轮询间隔(分)：", font=("微软雅黑", 9),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="left", padx=(10, 5))
        self.interval_var = tk.StringVar(value=str(self.interval_sec // 60))
        spin = tk.Spinbox(parent, from_=1, to=180, width=5,
                          textvariable=self.interval_var, font=("微软雅黑", 9),
                          bg=BG_INPUT, fg="#202124", relief="flat",
                          command=self._on_interval_change)
        spin.pack(side="left", ipady=4)
        self.interval_var.trace_add("write", lambda *_: self._on_interval_change())

        RoundedButton(parent, "清空新消息标记", command=self.clear_new_flags,
                      width=130, height=34, bg="#4B5563",
                      bg_parent=BG_DARK).pack(side="right", padx=25, pady=18)

    # ------------------------------------------------------------------ 列表
    def _render_list(self):
        """把通知渲染成卡片：先清空旧卡片，再按分类筛选、逐条渲染。"""
        for w in self.list_frame.winfo_children():
            w.destroy()

        if self.category == "全部":
            displayed = list(self.all_news)
        else:
            displayed = [n for n in self.all_news if n["category"] == self.category]

        # 尚无数据时：区分"正在抓取"和"确实没有消息"两种提示
        if not displayed:
            tip = "正在抓取最新通知，请稍候……" if not self.baseline_done else "（暂无消息）"
            tk.Label(self.list_frame, text=tip, font=("微软雅黑", 11),
                     bg=BG_DARK, fg=TEXT_SECOND).pack(pady=40)
            return

        for n in displayed:
            self._render_card(n)

    def _render_card(self, n):
        """把一条通知渲染成一张卡片：日期/分类/来源 + 标题 + 摘要 + 打开按钮。"""
        is_new = n["href"] in self.new_hrefs
        card_bg = "#243F73" if is_new else BG_CARD   # 新消息用更亮的背景
        card = tk.Frame(self.list_frame, bg=card_bg)
        card.pack(fill="x", pady=4)

        # 第一行：日期 + 分类胶囊 + 来源 + （新消息时）红色"● 新"
        top_row = tk.Frame(card, bg=card_bg)
        top_row.pack(fill="x", padx=14, pady=(8, 4))

        tk.Label(top_row, text=n["date"], font=("微软雅黑", 9),
                 bg=card_bg, fg=TEXT_SECOND).pack(side="left")

        cat_color = CATEGORY_COLORS.get(n["category"], "#6B7280")
        CategoryChip(top_row, n["category"], color=cat_color,
                     bg_parent=card_bg).pack(side="left", padx=8)

        if n["source"]:
            extra = f"来源：{n['source']}"
            if n.get("meta"):
                extra += f"   {n['meta']}"
            tk.Label(top_row, text=extra, font=("微软雅黑", 8),
                     bg=card_bg, fg=TEXT_SECOND).pack(side="left", padx=4)

        if is_new:
            tk.Label(top_row, text="● 新", font=("微软雅黑", 9, "bold"),
                     bg=card_bg, fg=ACCENT_RED).pack(side="right")

        # 第二行：标题（粗体，自动换行）
        tk.Label(card, text=n["title"], font=("微软雅黑", 11, "bold"),
                 bg=card_bg, fg=TEXT_PRIMARY, anchor="w", justify="left",
                 wraplength=760).pack(fill="x", padx=14, pady=(0, 4))

        # 第三行：摘要（超过 140 字截断）
        if n["summary"]:
            s = n["summary"]
            if len(s) > 140:
                s = s[:140] + "…"
            tk.Label(card, text=s, font=("微软雅黑", 9),
                     bg=card_bg, fg=TEXT_SECOND, anchor="w", justify="left",
                     wraplength=760).pack(fill="x", padx=14, pady=(0, 4))

        # 第四行：链接 + "打开"按钮
        link_row = tk.Frame(card, bg=card_bg)
        link_row.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(link_row, text=n["href"], font=("微软雅黑", 8),
                 bg=card_bg, fg=ACCENT_BLUE, anchor="w",
                 wraplength=760).pack(side="left", fill="x", expand=True)
        RoundedButton(link_row, "打开",
                      command=lambda u=n["href"]: self.open_url(u),
                      width=55, height=24, bg=ACCENT_BLUE,
                      font=("微软雅黑", 8), bg_parent=card_bg
                      ).pack(side="right")

    def open_url(self, url):
        """用系统默认浏览器打开链接。"""
        import webbrowser
        webbrowser.open(url)

    # ------------------------------------------------------------------ 交互
    def select_category(self, cat):
        """选择分类筛选并重建界面（与大监控窗口一致：重建以更新按钮高亮）。"""
        self.category = cat
        self.build_main()

    def manual_refresh(self):
        """手动刷新：后台线程执行，避免卡住界面。"""
        threading.Thread(target=lambda: self._fetch_task(self._first_run()),
                         daemon=True).start()

    def _first_run(self):
        """本次抓取是否应视为"首次基线抓取"：基线尚未建立时为 True。"""
        return not self.baseline_done

    def _on_interval_change(self):
        """间隔输入变化时解析数字（限制 1~180 分钟），并更新状态栏。"""
        try:
            v = int(self.interval_var.get())
            if 1 <= v <= 180:
                self.interval_sec = v * 60
                self.status_label.config(text=self._status_text())
        except ValueError:
            pass

    def clear_new_flags(self):
        """清空"新消息"标记并刷新。"""
        self.new_hrefs.clear()
        self._update_title()
        self._render_list()

    # ------------------------------------------------------------------ 后台
    def _start_worker(self):
        """启动后台轮询守护线程（主程序退出时自动结束）。"""
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def _worker_loop(self):
        """后台主循环：抓取 -> 按间隔等待；每秒检查 running 以便快速退出。"""
        time.sleep(0.5)
        while self.running:
            self._fetch_task(self._first_run())
            for _ in range(self.interval_sec):
                if not self.running:
                    return
                time.sleep(1)

    def _fetch_task(self, is_first):
        """后台抓取：抓取数据 -> 与已见链接对比 -> 回主线程刷新界面。

        is_first=True 时（基线尚未建立），把现有全部链接记入已见文件，
        不标记为新消息；之后只对真正的新链接发提醒。
        """
        old_seen = self._load_seen()
        if not self._do_fetch():
            return   # 抓取失败（已弹窗），本次不更新已见记录

        cur_hrefs = {n["href"] for n in self.all_news}
        if is_first:
            new_hrefs = set()
            self.baseline_done = True
        else:
            new_hrefs = cur_hrefs - old_seen
        # 合并链接并保存，供下次对比
        self._save_seen(old_seen | cur_hrefs)
        if new_hrefs:
            self.new_hrefs |= new_hrefs

        self.last_check_time = datetime.now()
        self.root.after(0, lambda: self._after_fetch(new_hrefs))

    def _after_fetch(self, new_hrefs):
        """主线程执行：刷新界面，有新通知则提醒。"""
        self.status_label.config(text=self._status_text())
        self._render_list()
        self.header_label.config(text=self._title_text())   # 只更新标题条数
        self._update_title()

        if new_hrefs:
            self.root.bell()
            try:
                self.root.attributes("-topmost", True)
                self.root.after(300, lambda: self.root.attributes("-topmost", False))
            except Exception:
                pass
            first = next((n for n in self.all_news if n["href"] in new_hrefs), None)
            tip = f"检测到 {len(new_hrefs)} 条新通知！\n\n"
            if first:
                tip += f"[{first['category']}] {first['title']}"
            tip += "\n\n（新通知已用红色「● 新」标记，可点击「清空新消息标记」消除）"
            messagebox.showinfo("新消息提醒", tip, parent=self.root)

    def _update_title(self):
        """窗口标题：有新消息时显示 [N 条新消息]。"""
        if self.new_hrefs:
            self.root.title(f"[{len(self.new_hrefs)} 条新消息] {WINDOW_TITLE}")
        else:
            self.root.title(WINDOW_TITLE)

    # ------------------------------------------------------------------ 关闭
    def on_close(self):
        """关闭窗口：停止后台线程并销毁窗口。"""
        self.running = False
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = PsychMonitorApp(root)
    root.mainloop()
