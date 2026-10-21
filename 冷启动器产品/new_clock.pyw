import tkinter as tk
from tkinter import font, simpledialog, messagebox, ttk
import sys
import os
import json
import ctypes
from datetime import datetime, timedelta
from ctypes import wintypes
import threading
import socket
import uuid
import time
import base64
import urllib.parse
import urllib.error
import http.server
import http.client

try:
    import pystray
    from pystray import MenuItem as Item
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    print("提示：pip install pystray pillow 可启用托盘功能")

NEXT_TASK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "next_task.txt")
CYCLE_COUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cycle_count.txt")
AGILE_TASKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agile_tasks.json")
# 云同步相关文件（GitHub 数据箱）
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
SYNC_CFG_FILE = os.path.join(_APP_DIR, "sync_config.json")
TRACKING_DEL_FILE = os.path.join(_APP_DIR, "deleted_track.json")
AGILE_DEL_FILE = os.path.join(_APP_DIR, "deleted_agile.json")
# 侧边色条按键尺寸：宽=一个计数格子(28)，长=三个格子(96)，与计数格子同高

GRID_ROWS = 3
GRID_COLS = 6
GRID_CELL_SIZE = 28
GRID_GAP = 6
TOTAL_CYCLES = GRID_ROWS * GRID_COLS

# 三级颜色（从上到下：紫、黄、绿）
LEVEL_COLORS = ["#9c27b0", "#ffc107", "#4caf50"]
EMPTY_COLOR = "#ffffff"
GRID_BG = "#222222"

# Windows API 常量
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000


def set_window_click_through(hwnd, enable=True):
    """
    设置窗口是否点击穿透（仅 Windows）。
    enable=True  → 鼠标穿透，点不到窗口
    enable=False → 正常，可以点击
    """
    if sys.platform != "win32":
        return
    ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enable:
        ex_style = ex_style | WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        ex_style = ex_style & ~WS_EX_TRANSPARENT
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)


class CountdownClock:
    def __init__(self, root):
        self.root = root
        self.root.title("冷启动器")
        self.root.configure(bg="#222222")

        self.window_w = 320  # 容纳左右两个窄色条按键
        self.window_h = 420

        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - self.window_w - 20
        y = screen_h - self.window_h - 60
        self.root.geometry(f"{self.window_w}x{self.window_h}+{x}+{y}")

        self.phase = 0
        self.remaining = 0
        self.total = 0
        self.running = False

        # 长计时（右键“开始”触发）：4 段 × 20 分钟 = 80 分钟，仅最后响一次提示音
        LONG_SEGMENT_MINUTES = 20
        self.long_mode = False
        self.long_stages_count = 4
        self.long_segment_seconds = LONG_SEGMENT_MINUTES * 60
        self.stage_index = 0

        self.cycle_count = self._load_cycle_count()

        grid_w = GRID_COLS * GRID_CELL_SIZE + (GRID_COLS - 1) * GRID_GAP
        grid_h = GRID_ROWS * GRID_CELL_SIZE + (GRID_ROWS - 1) * GRID_GAP

        # 顶部三栏：左=追踪任务色条(绿) | 中=计数格子 | 右=敏捷任务色条(白)
        self.panel_bg = "#222222"   # 顶部框架背景色（随阶段切换）
        self.top_frame = tk.Frame(root, bg=self.panel_bg)
        self.top_frame.pack(pady=(15, 5))

        self.left_bar = self._build_task_bar(self.top_frame, "#4caf50", grid_h, self._on_tracking_bar,
                                             padx=(0, GRID_CELL_SIZE // 2))
        self.grid_canvas = tk.Canvas(
            self.top_frame,
            width=grid_w,
            height=grid_h,
            bg=self.panel_bg,
            highlightthickness=0
        )
        self.grid_canvas.pack(side="left")
        self.right_bar = self._build_task_bar(self.top_frame, "#ffffff", grid_h, self._on_agile_bar,
                                              padx=(GRID_CELL_SIZE // 2, 0))

        self.grid_cells = []
        self._create_grid_cells()
        self._update_grid_colors()
        self.grid_canvas.bind("<Button-3>", self.reset_cycle_count)

        # 加载敏捷任务数据；敏捷任务界面窗口（点击色条/按键后打开）
        self.agile_tasks = self._load_agile_tasks()
        self.agile_window = None
        # 追踪任务界面窗口（点击左侧绿色条后打开）
        self.tracking_window = None

        self.tray_icon = None
        self._hidden_to_tray = False
        # 浮窗对象
        self.float_window = None
        # 竖向进度条独立窗口
        self.progress_window = None
        # 进度条下方的敏捷任务入口按键窗口
        self.agile_btn_window = None

        self.time_font = font.Font(family="Helvetica", size=64, weight="bold")
        self.time_label = tk.Label(
            root,
            text="02:00",
            font=self.time_font,
            fg="#ff4444",
            bg="#222222"
        )
        self.time_label.pack(expand=True, fill="both", pady=20)

        self.phase_label = tk.Label(
            root,
            text="点击按钮开始",
            font=("Helvetica", 14),
            fg="#aaaaaa",
            bg="#222222"
        )
        self.phase_label.pack(pady=2)

        self.next_task_panel = RoundedPanel(root, height=48, radius=10,
                                            fill="#2c2c2c", padx=12, pady=5)
        self.next_task_panel.pack(pady=5, fill="x", padx=14)
        self.next_task_label = tk.Label(
            self.next_task_panel.body,
            text="",
            font=("Helvetica", 11),
            fg="#ffcc66",
            bg="#2c2c2c",
            wraplength=268,
            justify="center"
        )
        self.next_task_label.pack(fill="x", pady=3)
        self.load_next_task()

        self.button = RoundedButton(
            root, text="开始", command=self.start_countdown,
            command_right=self.start_long_countdown,
            fnt=("Helvetica", 16, "bold"), bg="#e53935", fg="white",
            radius=16, padx=30, pady=12
        )
        self.button.pack(pady=10)

        self.taskbar = None
        self.root.after(100, self.init_taskbar)

        if HAS_TRAY:
            self.root.after(200, self.init_tray)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self.on_minimize)

        # ==================== 手机端（局域网）Web 服务 ====================
        self.mobile_server = None      # _MobileHTTPServer 实例
        self.mobile_url = ""           # 手机访问地址，如 http://192.168.1.5:8765
        self._remote_dirty = False     # Web 线程置位，主线程轮询后刷新界面
        self.agile_tooltip = None      # 敏捷任务悬停提示窗
        self._tip_job = None           # 提示窗延迟隐藏任务
        self.root.after(300, self._start_mobile_server)
        self.root.after(1000, self._poll_remote_changes)
        self._sync_stop = threading.Event()   # 云同步线程的停止信号
        self.root.after(1500, self._start_cloud_sync)

    # ==================== 托盘相关方法 ====================

    def init_tray(self):
        image = self._create_tray_image()
        menu = (
            Item('显示窗口', self._tray_show, default=True),
            Item('退出程序', self._tray_quit)
        )
        self.tray_icon = pystray.Icon("cold_starter", image, "冷启动器", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _create_tray_image(self):
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(68, 136, 255, 255))
        draw.ellipse([12, 12, 52, 52], fill=(34, 34, 34, 255))
        draw.line([32, 32, 32, 16], fill=(255, 204, 102, 255), width=3)
        draw.line([32, 32, 44, 38], fill=(255, 255, 255, 255), width=2)
        return img

    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self._show_from_tray)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self._quit_app)

    def _show_from_tray(self):
        """从托盘恢复显示窗口"""
        self._destroy_float_window()
        self._destroy_progress_window()
        self._hidden_to_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.root.after(200, lambda: self.root.attributes('-topmost', False))
        self.root.focus_force()

    def _hide_to_tray(self):
        if not HAS_TRAY or self._hidden_to_tray:
            return
        self._hidden_to_tray = True
        # 主窗口最小化到托盘时，一并彻底关闭敏捷任务窗口与长期任务窗口
        self._hide_tooltip()
        self._close_agile_window()
        self._close_tracking_window()
        self.root.withdraw()
        if self.tray_icon:
            self.tray_icon.notify("冷启动器", "已最小化到托盘，计时继续中")
        # 显示右上角任务浮窗
        self._create_float_window()
        # 第一/二阶段显示竖向进度条窗口（第一阶段红、第二阶段蓝）
        if self.running and self.phase == 1:
            self._create_progress_window(color="#ff4444", label_fg="#ff6666")
        elif self.running and self.phase == 2:
            self._create_progress_window(color="#4488ff", label_fg="#66aaff")

    def on_minimize(self, event):
        if self.root.state() == 'iconic' and not self._hidden_to_tray:
            self.root.after(50, self._hide_to_tray)

    def on_close(self):
        if HAS_TRAY:
            self._hide_to_tray()
        else:
            self._quit_app()

    def _quit_app(self):
        """真正退出程序"""
        self._destroy_float_window()
        self._destroy_progress_window()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()

    # ==================== 托盘浮窗（任务提示） ====================

    def _create_float_window(self):
        """创建右上角悬浮小窗口，显示下一个任务，真正点击穿透"""
        if self.float_window:
            return

        self.float_window = tk.Toplevel(self.root)
        self.float_window.overrideredirect(True)
        self.float_window.attributes('-topmost', True)
        self.float_window.attributes('-alpha', 0.9)

        fw, fh = 260, 60

        screen_w = self.float_window.winfo_screenwidth()
        x = screen_w - fw - 10
        y = 10
        self.float_window.geometry(f"{fw}x{fh}+{x}+{y}")
        self.float_window.configure(bg="#1a1a2e")

        # 圆角卡片：标题 + 任务文字
        card = RoundedPanel(self.float_window, height=fh - 6, radius=14,
                            fill="#23233a", padx=10, pady=4)
        card.pack(fill="both", expand=True, padx=3, pady=3)

        tk.Label(card.body, text="▸ 下一个任务", font=("Helvetica", 9),
                 fg="#ffcc66", bg="#23233a").pack(anchor="w", pady=(2, 0))

        task_text = ""
        try:
            if os.path.exists(NEXT_TASK_FILE):
                with open(NEXT_TASK_FILE, "r", encoding="utf-8") as f:
                    task_text = f.read().strip()
        except:
            pass

        self.float_task_label = tk.Label(
            card.body,
            text=task_text if task_text else "（暂无任务）",
            font=("Helvetica", 11),
            fg="#ffffff",
            bg="#23233a",
            wraplength=fw - 40,
            justify="left",
            anchor="w"
        )
        self.float_task_label.pack(anchor="w", pady=(0, 3), fill="x")

        # ===== 真正的点击穿透：用 Windows API =====
        self.float_window.update_idletasks()
        hwnd = int(self.float_window.frame(), 16)
        set_window_click_through(hwnd, True)

    def _destroy_float_window(self):
        """销毁任务浮窗"""
        if self.float_window:
            self.float_window.destroy()
            self.float_window = None

    # ==================== 竖向进度条独立窗口 ====================

    def _create_progress_window(self, color="#ff4444", label_fg="#ff6666"):
        """创建独立的竖向进度条窗口，放在屏幕中间偏右，真正点击穿透
        color: 填充色（第一阶段红 #ff4444，第二阶段蓝 #4488ff）
        """
        if self.progress_window:
            return

        self.prog_fill_color = color
        self.prog_label_fg = label_fg

        self.progress_window = tk.Toplevel(self.root)
        self.progress_window.overrideredirect(True)
        self.progress_window.attributes('-topmost', True)
        self.progress_window.attributes('-alpha', 0.85)

        pw, ph = 36, 260

        screen_w = self.progress_window.winfo_screenwidth()
        screen_h = self.progress_window.winfo_screenheight()
        x = screen_w - pw - 10           # 距离右边 80px
        y = (screen_h - ph) // 2         # 垂直居中
        self.progress_window.geometry(f"{pw}x{ph}+{x}+{y}")
        self.progress_window.configure(bg="#1a1a2e")

        # 进度条 Canvas
        bar_w = pw - 8   # 28
        bar_h = ph - 16  # 244
        self.prog_bar_canvas = tk.Canvas(
            self.progress_window,
            width=bar_w,
            height=bar_h,
            bg="#1a1a2e",
            highlightthickness=0
        )
        self.prog_bar_canvas.pack(pady=8, padx=4)

        # 背景槽
        self.prog_bar_bg = self.prog_bar_canvas.create_rectangle(
            2, 2, bar_w - 2, bar_h - 2,
            fill="#333355", outline="#555577", width=1
        )
        # 填充（从下往上）
        self.prog_bar_fill = self.prog_bar_canvas.create_rectangle(
            2, bar_h - 2, bar_w - 2, bar_h - 2,
            fill=color, outline=""
        )

        # 长计时：按 4 段等分绘制分隔线（覆盖在填充之上，便于看清段落）
        if self.long_mode:
            for i in range(1, self.long_stages_count):
                div_y = 2 + bar_h * i / self.long_stages_count
                self.prog_bar_canvas.create_line(
                    2, div_y, bar_w - 2, div_y, fill="#8888aa", width=1
                )

        # 顶部小标签
        self.prog_bar_label = tk.Label(
            self.progress_window,
            text="",
            font=("Helvetica", 7, "bold"),
            fg=label_fg,
            bg="#1a1a2e"
        )
        self.prog_bar_label.place(x=0, y=2, width=pw, anchor="n")

        # ===== 真正的点击穿透：用 Windows API =====
        self.progress_window.update_idletasks()
        hwnd = int(self.progress_window.frame(), 16)
        set_window_click_through(hwnd, True)

        # 进度条下方的小方块按键：点击打开敏捷任务界面（独立小窗口，不穿透、可点击）
        # 与进度条居中对齐，颜色跟随计时条（一阶段红 #ff4444 / 二阶段蓝 #4488ff）
        self.agile_btn_window = tk.Toplevel(self.root)
        self.agile_btn_window.overrideredirect(True)
        self.agile_btn_window.attributes("-topmost", True)
        self.agile_btn_window.configure(bg="#1a1a2e")
        btn_size = 24
        btn_x = x + (pw - btn_size) // 2   # 相对进度条水平居中
        self.agile_btn_window.geometry(f"{btn_size}x{btn_size}+{btn_x}+{y + ph + 6}")
        self.agile_btn = tk.Frame(
            self.agile_btn_window, width=btn_size, height=btn_size,
            bg=color, highlightthickness=1,
            highlightbackground="#555555", cursor="hand2"
        )
        self.agile_btn.pack()
        self.agile_btn.bind("<Button-1>", lambda e: self._open_agile_window())

        # 立即更新一次
        self._update_progress_window()

    def _update_progress_window(self):
        """更新竖向进度条窗口"""
        if not self.progress_window or not hasattr(self, 'prog_bar_fill'):
            return
        if self.phase not in (1, 2) or not self.running:
            return

        bar_h = 244  # ph - 16 = 260 - 16
        elapsed = self.total - self.remaining
        if self.long_mode and self.long_segment_seconds > 0:
            # 长计时：整条代表 80 分钟，跨段累计
            total_all = self.long_segment_seconds * self.long_stages_count
            elapsed_all = self.stage_index * self.long_segment_seconds + elapsed
            ratio = elapsed_all / total_all
        else:
            ratio = elapsed / self.total if self.total > 0 else 0
        fill_h = int(bar_h * ratio)

        top_y = 2 + (bar_h - fill_h)
        self.prog_bar_canvas.coords(
            self.prog_bar_fill,
            2, top_y, 26, bar_h  # bar_w - 2 = 26
        )

        # 更新剩余时间文字
        mins = self.remaining // 60
        secs = self.remaining % 60
        self.prog_bar_label.config(text=f"{mins:01d}:{secs:02d}")

    def _destroy_progress_window(self):
        """销毁竖向进度条窗口及其下方的敏捷入口按键"""
        if self.agile_btn_window:
            self.agile_btn_window.destroy()
            self.agile_btn_window = None
        if self.progress_window:
            self.progress_window.destroy()
            self.progress_window = None

    # ==================== 侧边色条按键 & 敏捷任务界面 ====================

    def _build_task_bar(self, parent, color, height, command, padx=0):
        """构建圆角无文字色条按键：宽=一个计数格子(28)，长=三个格子(96)，与计数格子同高
        padx: 与计数格子的水平间距（半格=14px）
        """
        bar = RoundedBar(parent, width=GRID_CELL_SIZE, height=height,
                         color=color, command=command, radius=10)
        bar.pack(side="left", padx=padx)
        return bar

    def _on_tracking_bar(self):
        """左侧绿色条：打开追踪任务（OKR）界面"""
        self._open_tracking_window()

    def _open_tracking_window(self):
        """打开追踪任务界面（非计时阶段可编辑，计时阶段只读）"""
        if self.tracking_window is not None and self.tracking_window.winfo_exists():
            self.tracking_window.lift()
            self.tracking_window.focus_force()
            return
        self.tracking_window = TrackingWindow(self.root, on_close=self._on_tracking_closed)
        self._apply_tracking_lock()

    def _on_tracking_closed(self):
        """追踪任务窗口关闭回调"""
        self.tracking_window = None

    def _close_tracking_window(self):
        """关闭追踪任务窗口（若已打开）"""
        if self.tracking_window is not None and self.tracking_window.winfo_exists():
            self.tracking_window.close()
        self.tracking_window = None

    def _apply_tracking_lock(self):
        """把当前计时状态同步到追踪任务界面（计时中锁定编辑，仅可查看）"""
        locked = self.running and self.phase in (1, 2)
        if self.tracking_window is not None and self.tracking_window.winfo_exists():
            self.tracking_window.set_locked(locked)

    def _on_agile_bar(self):
        """右侧白色条：打开敏捷任务界面"""
        self._open_agile_window()

    # ---------- 敏捷任务界面（独立窗口） ----------

    def _open_agile_window(self):
        """打开敏捷任务界面：任务列表(每行仅一个删除键) + 底部输入条(回车添加)"""
        if self.agile_window is not None and self.agile_window.winfo_exists():
            self.agile_window.lift()
            self.agile_window.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("敏捷任务")
        win.configure(bg="#222222")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.update_idletasks()
        w, h = 240, 300
        sx = win.winfo_screenwidth()
        sy = win.winfo_screenheight()
        # 贴在屏幕右边缘、主窗口正上方，与主窗口顶部留出间隔（避免遮住标题栏/最小化按钮）
        main_w, main_h, margin = 320, 420, 20
        px = sx - w - margin            # 右边缘与主窗口对齐
        main_top = sy - main_h - 60     # 主窗口顶边 y（与 __init__ 中主窗口定位一致）
        py = max(0, main_top - h - 36)  # 再上移 36px，避开主窗口的最小化按钮
        win.geometry(f"{w}x{h}+{px}+{py}")
        win.protocol("WM_DELETE_WINDOW", self._close_agile_window)
        # 鼠标滚轮上下滚动任务列表（事件会经 bindtags 冒泡到此窗口，覆盖全部子控件）
        win.bind("<MouseWheel>", self._on_agile_mousewheel)
        self.agile_window = win

        # 任务列表区（可滚动）；先创建，稍后再 pack，确保底部输入条优先占位
        list_frame = tk.Frame(win, bg="#222222")

        self.agile_canvas = tk.Canvas(list_frame, bg="#222222", highlightthickness=0)
        self.agile_scroll = tk.Scrollbar(
            list_frame, orient="vertical", command=self.agile_canvas.yview
        )
        self.agile_canvas.configure(yscrollcommand=self.agile_scroll.set)
        self.agile_inner = tk.Frame(self.agile_canvas, bg="#222222")
        self.agile_inner.bind(
            "<Configure>",
            lambda e: self.agile_canvas.configure(scrollregion=self.agile_canvas.bbox("all"))
        )
        self.agile_canvas.create_window(
            (0, 0), window=self.agile_inner, anchor="nw", tags=("agile_window",)
        )
        self.agile_canvas.bind(
            "<Configure>",
            lambda e: self.agile_canvas.itemconfig("agile_window", width=e.width)
        )
        self.agile_canvas.pack(side="left", fill="both", expand=True)
        self.agile_scroll.pack(side="right", fill="y")

        # 底部提示：手机端访问地址（同一 WiFi 下手机浏览器打开即可查看/增删改）
        if self.mobile_url:
            tk.Label(
                win, text=f"手机端：{self.mobile_url}",
                font=("Helvetica", 8), fg="#7a7a7a", bg="#222222"
            ).pack(side="bottom", fill="x", padx=6, pady=(0, 4))

        # 底部输入条：回车即添加（无独立添加按键）
        self.agile_entry = RoundedEntry(
            win, height=28, radius=10, fnt=("Helvetica", 10),
            bg="#333333", fg="#ffffff", insertbackground="#ffffff")
        self.agile_entry.pack(side="bottom", fill="x", padx=6, pady=6)
        self.agile_entry.bind("<Return>", self._add_agile_task)
        # 输入限制：最多 15 个字，避免文字过长把删除键挤出窗口
        self.agile_entry.config(
            validate="key",
            validatecommand=(self.root.register(lambda s: len(s) <= 15), "%P"))
        self.agile_entry.focus_set()

        # 输入条已占位，列表区填充剩余空间
        list_frame.pack(side="top", fill="both", expand=True, padx=6, pady=6)

        self._render_agile_tasks()

    def _close_agile_window(self):
        """关闭敏捷任务界面"""
        if self.agile_window is not None and self.agile_window.winfo_exists():
            self.agile_window.destroy()
        self.agile_window = None

    def _on_agile_mousewheel(self, event):
        """鼠标滚轮滚动敏捷任务列表（Windows 上 delta 为 120 的整数倍）"""
        if self.agile_window is None or not self.agile_window.winfo_exists():
            return
        steps = int(-event.delta / 120)
        if steps:
            self.agile_canvas.yview_scroll(steps, "units")

    def _render_agile_tasks(self):
        """重绘敏捷任务列表：每行 = 任务文本 + 右侧删除键；鼠标悬停显示添加时间"""
        if self.agile_window is None or not self.agile_window.winfo_exists():
            return
        for w in self.agile_inner.winfo_children():
            w.destroy()
        for i, task in enumerate(self.agile_tasks):
            row = RoundedPanel(self.agile_inner, height=30, radius=9,
                               fill="#2a2a2a", padx=6, pady=2)
            row.pack(fill="x", pady=2)
            lbl = tk.Label(
                row.body, text=task.get("text", ""), font=("Helvetica", 10),
                fg="#ffffff", bg="#2a2a2a", anchor="w"
            )
            lbl.pack(side="left", fill="x", expand=True)
            del_btn = tk.Button(
                row.body, text="✕", width=2, font=("Helvetica", 8),
                relief="flat", bg="#3a3a3a", fg="#ff6666",
                activebackground="#555555", cursor="hand2",
                command=lambda i=i: self._delete_agile_task(i)
            )
            del_btn.pack(side="right", padx=(2, 0))
            # 悬停提示：添加时间（老数据没有时间则显示“未知”）
            tip_text = f"添加时间：{task.get('created_at') or '（旧任务，无记录）'}"
            for w in (row, row.body, lbl, del_btn):
                self._bind_tooltip(w, tip_text)

    def _bind_tooltip(self, widget, text):
        """给控件绑定悬停提示（多个子控件共用一个提示窗，避免闪烁）"""
        widget.bind("<Enter>", lambda e, t=text: self._show_tooltip(widget, t), add="+")
        widget.bind("<Leave>", lambda e: self._schedule_hide_tooltip(), add="+")

    def _show_tooltip(self, widget, text):
        if self._tip_job is not None:
            self.root.after_cancel(self._tip_job)
            self._tip_job = None
        self._hide_tooltip()
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        try:
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            tip.wm_geometry(f"+{x}+{y}")
        except Exception:
            pass
        tk.Label(tip, text=text, bg="#000000", fg="#ffffff",
                 font=("Helvetica", 9), padx=6, pady=3).pack()
        self.agile_tooltip = tip

    def _schedule_hide_tooltip(self):
        """离开后再延迟隐藏，避免在行内子控件间移动时闪烁"""
        if self._tip_job is not None:
            self.root.after_cancel(self._tip_job)
        self._tip_job = self.root.after(120, self._hide_tooltip)

    def _hide_tooltip(self):
        self._tip_job = None
        if self.agile_tooltip is not None:
            try:
                self.agile_tooltip.destroy()
            except Exception:
                pass
            self.agile_tooltip = None

    def _add_agile_task(self, event=None):
        """回车添加敏捷任务（可随时添加，无计时限制），自动记录添加时间"""
        text = self.agile_entry.get().strip()
        if not text:
            return
        text = text[:15]  # 兜底：最多 15 个字
        self.agile_tasks.append(new_agile_task(text))
        self.agile_entry.delete(0, tk.END)
        self._save_agile_tasks()
        self._render_agile_tasks()
        if self.agile_window is not None and self.agile_window.winfo_exists():
            self.agile_canvas.yview_moveto(1.0)  # 滚到底部，新任务立即可见

    def _delete_agile_task(self, index):
        """删除指定敏捷任务"""
        if 0 <= index < len(self.agile_tasks):
            self.agile_tasks.pop(index)
            self._save_agile_tasks()
            self._render_agile_tasks()

    def _load_agile_tasks(self):
        """读取敏捷任务数据（统一为 {id,text,created_at} 结构，兼容旧版字符串数组）"""
        return load_agile_tasks()

    def _save_agile_tasks(self):
        """保存敏捷任务数据"""
        save_agile_tasks(self.agile_tasks)

    # ---------- 手机端 Web 服务 & 数据同步 ----------

    def _get_lan_ip(self):
        """获取本机局域网 IP（用于手机访问）"""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # 不会真正发包，仅用于取本机出口 IP
            return s.getsockname()[0]
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"
        finally:
            if s is not None:
                s.close()

    def _start_mobile_server(self):
        """启动手机端局域网 Web 服务（后台线程）"""
        try:
            self.mobile_server = _MobileHTTPServer(
                ("0.0.0.0", MOBILE_PORT), MobileTaskHandler,
                on_change=self._on_remote_change)
            threading.Thread(
                target=self.mobile_server.serve_forever, daemon=True).start()
            self.mobile_url = f"http://{self._get_lan_ip()}:{MOBILE_PORT}"
            print("手机端已启动，同一 WiFi 下用手机浏览器访问：", self.mobile_url)
        except Exception as e:
            self.mobile_url = ""
            print("手机端服务启动失败：", e)

    def _on_remote_change(self):
        """由 Web 线程回调：仅置位标记，不直接操作 Tk 控件"""
        self._remote_dirty = True

    def _start_cloud_sync(self):
        """启动云同步后台线程（未配置 token 时自动跳过）"""
        cfg = load_sync_config()
        if not cfg.get("token"):
            print("云同步未启用：请在 sync_config.json 里填写 GitHub 令牌")
            return
        threading.Thread(target=self._cloud_sync_loop, daemon=True).start()
        print("云同步已启用，数据箱：", cfg["owner"] + "/" + cfg["repo"] + "/" + cfg["path"])

    def _cloud_sync_loop(self):
        """后台循环：每 30 秒同步一次；若本地有更新则通知主线程刷新界面"""
        while not self._sync_stop.is_set():
            try:
                if sync_once():
                    self._remote_dirty = True
            except Exception as e:
                print("云同步线程异常:", e)
            self._sync_stop.wait(30)

    def _poll_remote_changes(self):
        """主线程轮询：手机端修改过数据时刷新桌面界面"""
        if self._remote_dirty:
            self._remote_dirty = False
            self._refresh_from_disk()
        self.root.after(1000, self._poll_remote_changes)

    def _refresh_from_disk(self):
        """手机端改动后，从磁盘重新加载并刷新已打开的窗口"""
        self.agile_tasks = self._load_agile_tasks()
        if self.agile_window is not None and self.agile_window.winfo_exists():
            self._render_agile_tasks()
        if self.tracking_window is not None and self.tracking_window.winfo_exists():
            self.tracking_window.reload_from_disk()

    def _theme_panels(self):
        """按当前阶段背景色刷新顶部框架（左右色条保持固定色，不随阶段变化）"""
        self.top_frame.config(bg=self.panel_bg)

    # ========== 创建网格格子 ==========

    def _create_grid_cells(self):
        for row in range(GRID_ROWS):
            row_cells = []
            for col in range(GRID_COLS):
                x1 = col * (GRID_CELL_SIZE + GRID_GAP)
                y1 = row * (GRID_CELL_SIZE + GRID_GAP)
                x2 = x1 + GRID_CELL_SIZE
                y2 = y1 + GRID_CELL_SIZE
                rect = self.grid_canvas.create_rectangle(
                    x1, y1, x2, y2,
                    fill=EMPTY_COLOR,
                    outline="#555555",
                    width=2
                )
                row_cells.append(rect)
            self.grid_cells.append(row_cells)

    # ========== 进位制更新网格颜色 ==========

    def _update_grid_colors(self):
        count = self.cycle_count

        purple = count // 36
        yellow = (count // 6) % 6
        green = count % 6

        level_counts = [purple, yellow, green]

        for row in range(GRID_ROWS):
            fill_count = min(level_counts[row], GRID_COLS)
            for col in range(GRID_COLS):
                rect = self.grid_cells[row][col]
                if col < fill_count:
                    self.grid_canvas.itemconfig(rect, fill=LEVEL_COLORS[row])
                else:
                    self.grid_canvas.itemconfig(rect, fill=EMPTY_COLOR)

    # ========== 读取/保存计数 ==========

    def _load_cycle_count(self):
        try:
            if os.path.exists(CYCLE_COUNT_FILE):
                with open(CYCLE_COUNT_FILE, "r", encoding="utf-8") as f:
                    return int(f.read().strip())
        except Exception as e:
            print("读取 cycle_count.txt 失败:", e)
        return 0

    def _save_cycle_count(self):
        try:
            with open(CYCLE_COUNT_FILE, "w", encoding="utf-8") as f:
                f.write(str(self.cycle_count))
        except Exception as e:
            print("写入 cycle_count.txt 失败:", e)

    def reset_cycle_count(self, event=None):
        self.cycle_count = 0
        self._save_cycle_count()
        self._update_grid_colors()

    def _increment_cycle(self):
        self.cycle_count += 1
        self._save_cycle_count()
        self._update_grid_colors()

    # ==================== 读取/保存任务 ====================

    def load_next_task(self):
        try:
            if os.path.exists(NEXT_TASK_FILE):
                with open(NEXT_TASK_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        self.next_task_label.config(text=f" 上次留下：{content}")
        except Exception as e:
            print("读取 next_task.txt 失败:", e)

    def save_next_task(self, text):
        try:
            with open(NEXT_TASK_FILE, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print("写入 next_task.txt 失败:", e)

    # ==================== 弹窗逻辑 ====================

    def show_next_task_dialog(self):
        if self._hidden_to_tray:
            self._show_from_tray()
            self.root.after(300, self._show_dialog)
        else:
            self.root.lift()
            self.root.attributes('-topmost', True)
            self.root.after(200, lambda: self.root.attributes('-topmost', False))
            self.root.focus_force()
            self._show_dialog()

    def _show_dialog(self):
        self._increment_cycle()

        dialog = tk.Toplevel(self.root)
        dialog.title("留下下一个任务")
        dialog.configure(bg="#222222")
        dialog.resizable(False, False)

        dialog.update_idletasks()
        dw, dh = 380, 200
        sx = dialog.winfo_screenwidth()
        sy = dialog.winfo_screenheight()
        dialog.geometry(f"{dw}x{dh}+{(sx-dw)//2}+{(sy-dh)//2}")
        dialog.transient(self.root)
        dialog.grab_set()

        dialog.attributes('-topmost', True)
        dialog.after(100, lambda: dialog.attributes('-topmost', False))

        tk.Label(
            dialog,
            text="下次从哪里接着干？\n写下一个任务的开头（一句话就行）",
            font=("Helvetica", 12),
            fg="#ffcc66",
            bg="#222222",
            justify="center"
        ).pack(pady=15)

        entry = tk.Entry(dialog, font=("Helvetica", 12), width=30)
        entry.pack(pady=5, ipady=4)
        hint_label = tk.Label(
            dialog,
            text="至少写10个字，方便下次接着干",
            font=("Helvetica", 9),
            fg="#888888",
            bg="#222222"
        )
        hint_label.pack(pady=0)
        entry.focus_set()

        def on_save():
            text = entry.get().strip()
            if len(text) < 10:
                entry.config(bg="#ffcccc")
                hint_label.config(text=f"至少写10个字哦，当前只有 {len(text)} 字", fg="#ff6666")
                self.root.after(500, lambda: entry.config(bg="white"))
                return
            self.save_next_task(text)
            self.next_task_label.config(text=f" 上次留下：{text}")
            dialog.destroy()
            self.reset_to_idle()

        def on_skip():
            hint_label.config(text="不能跳过哦，至少写10个字的下一个任务", fg="#ff6666")
            entry.config(bg="#ffcccc")
            self.root.after(500, lambda: entry.config(bg="white"))
            entry.focus_set()

        btn_frame = tk.Frame(dialog, bg="#222222")
        btn_frame.pack(pady=15)

        tk.Button(
            btn_frame, text="保存并关闭", command=on_save,
            bg="#4488ff", fg="white", font=("Helvetica", 11, "bold"),
            width=12, height=1, relief="raised", cursor="hand2"
        ).pack(side="left", padx=8)

        dialog.bind("<Return>", lambda e: on_save())
        dialog.bind("<Escape>", lambda e: on_skip())

    def reset_to_idle(self):
        self.running = False
        self.phase = 0
        self.long_mode = False
        self.stage_index = 0
        self.phase_label.config(text="全部完成！", fg="#66ff66")
        self.button.set_text("重新开始")
        self.button.set_bg("#e53935")
        self.root.configure(bg="#222222")
        self.time_label.config(bg="#222222", fg="#ff4444")
        self.phase_label.config(bg="#222222")
        self.next_task_panel.set_fill("#2c2c2c")
        self.time_label.config(text="02:00")
        self.grid_canvas.config(bg="#222222")
        self.panel_bg = "#222222"
        self._theme_panels()
        self._apply_tracking_lock()

        if self.taskbar:
            self.taskbar.hide()

    def init_taskbar(self):
        try:
            hwnd = int(self.root.frame(), 16)
            self.taskbar = TaskbarProgress(hwnd)
        except:
            self.taskbar = None

    def play_beep(self):
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
        else:
            self.root.bell()

    def start_countdown(self):
        if self.running:
            return
        self.long_mode = False
        self.stage_index = 0
        self.phase = 1
        self.total = 2 * 60        # 2分钟倒计时
        self.remaining = self.total
        self.running = True
        self.time_label.config(fg="#ff4444")
        self.phase_label.config(text="第一阶段：2分钟倒计时", fg="#ff6666")
        self.button.set_text("倒计时中...")
        self.button.set_bg("#999999")

        if self.taskbar:
            self.taskbar.set_state(2)
            self.taskbar.set_progress(0, self.total)

        self._apply_tracking_lock()
        self.tick()

    def start_long_countdown(self, event=None):
        """右键“开始”：开启 80 分钟长计时（4 段 × 20 分钟）
        仅在最后一段结束时响一次提示音；复用第二阶段的蓝色主题与托盘进度条
        """
        if self.running:
            return
        self.long_mode = True
        self.stage_index = 0
        self.phase = 2              # 复用第二阶段（蓝）主题与托盘进度条逻辑
        self.total = self.long_segment_seconds
        self.remaining = self.total
        self.running = True

        self.time_label.config(fg="#4488ff", bg="#112244")
        self.phase_label.config(
            text=f"长计时 第1/{self.long_stages_count}段：20分钟",
            fg="#66aaff", bg="#112244")
        self.root.configure(bg="#112244")
        self.next_task_panel.set_fill("#112244")
        self.grid_canvas.config(bg="#112244")
        self.panel_bg = "#112244"
        self._theme_panels()
        self.button.set_text("长计时中")
        self.button.set_bg("#1565c0")

        if self.taskbar:
            self.taskbar.hide()

        self._apply_tracking_lock()
        self._destroy_progress_window()
        if self._hidden_to_tray:
            self._create_progress_window(color="#4488ff", label_fg="#66aaff")
        self.tick()

    def tick(self):
        if not self.running:
            return

        if self.remaining > 0:
            mins = self.remaining // 60
            secs = self.remaining % 60
            self.time_label.config(text=f"{mins:02d}:{secs:02d}")

            if self.phase == 1 and self.taskbar:
                elapsed = self.total - self.remaining
                self.taskbar.set_progress(elapsed, self.total)

            # 更新独立竖向进度条窗口（第一/二阶段 + 已最小化）
            if self.phase in (1, 2) and self._hidden_to_tray and self.progress_window:
                self._update_progress_window()

            self.remaining -= 1
            self.root.after(1000, self.tick)
        else:
            self.time_label.config(text="00:00")
            self.switch_phase()

    def switch_phase(self):
        # 长计时：4 段依次推进，中间静默（不响铃），仅最后一段结束响一次
        if self.long_mode:
            self.stage_index += 1
            if self.stage_index < self.long_stages_count:
                self.total = self.long_segment_seconds
                self.remaining = self.total
                self.phase_label.config(
                    text=f"长计时 第{self.stage_index + 1}/{self.long_stages_count}段：20分钟")
                self.tick()
            else:
                self.play_beep()
                self.show_next_task_dialog()
            return

        if self.phase == 1:
            self.play_beep()

            self.phase = 2
            self.total = 20 * 60  # 20分钟倒计时
            self.remaining = self.total
            self.time_label.config(fg="#4488ff")
            self.phase_label.config(text="第二阶段：20分钟倒计时", fg="#66aaff")
            self.root.configure(bg="#112244")
            self.time_label.config(bg="#112244")
            self.phase_label.config(bg="#112244")
            self.next_task_panel.set_fill("#112244")
            self.grid_canvas.config(bg="#112244")
            self.panel_bg = "#112244"
            self._theme_panels()

            self.button.set_text("进行中")
            self.button.set_bg("#1565c0")

            if self.taskbar:
                self.taskbar.hide()

            # 第一阶段结束，销毁红色进度条窗口
            # 若在托盘最小化中，重建为蓝色进度条（第二阶段）
            self._destroy_progress_window()
            if self._hidden_to_tray and self.running:
                self._create_progress_window(color="#4488ff", label_fg="#66aaff")

            self.tick()

        elif self.phase == 2:
            self.play_beep()
            self.show_next_task_dialog()


# ================================================================
#  追踪任务（OKR）—— 数据与界面
# ================================================================

TRACKING_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "okr_data.json")
TRACKING_LOG_INTERVAL_DAYS = 3  # 进度自动记录间隔（天）

TRACKING_BG = "#0F2B5B"
TRACKING_BG_CARD = "#1A3A6B"
TRACKING_BG_INPUT = "#FFFFFF"
TRACKING_TEXT = "#FFFFFF"
TRACKING_TEXT_SEC = "#8BA4CC"
TRACKING_BORDER = "#2A4A7A"
TRACKING_CHART_BG = "#0A1F44"
TRACKING_CHART_GRID = "#1E3A6B"
TRACKING_ACCENT_BLUE = "#4A90FF"
TRACKING_ACCENT_GREEN = "#34D399"
TRACKING_ACCENT_RED = "#F87171"
TRACKING_ACCENT_YELLOW = "#FBBF24"

TRACKING_STATUS_COLORS = {
    "待办": "#6B7280",
    "正在处理": TRACKING_ACCENT_BLUE,
    "完成": TRACKING_ACCENT_GREEN,
    "跳过": TRACKING_ACCENT_RED,
}


def tracking_mix(bg_hex, fg_hex, alpha):
    """按比例混合两个十六进制颜色，返回新颜色"""
    bg_hex = bg_hex.lstrip("#")
    fg_hex = fg_hex.lstrip("#")
    br, bg_, bb = [int(bg_hex[i:i + 2], 16) for i in (0, 2, 4)]
    fr, fg_, fb = [int(fg_hex[i:i + 2], 16) for i in (0, 2, 4)]
    r = int(br + (fr - br) * alpha)
    g = int(bg_ + (fg_ - bg_) * alpha)
    b = int(bb + (fb - bb) * alpha)
    return f"#{r:02x}{g:02x}{b:02x}"


def tracking_rounded_rect(canvas, x1, y1, x2, y2, r, color, tag=None):
    """在 canvas 上画一个圆角矩形（smooth 多边形），可指定 tag 便于局部重绘"""
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
              x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    canvas.create_polygon(points, fill=color, smooth=True, outline=color, tags=tag)


class RoundedButton(tk.Canvas):
    """圆角按键：Canvas 绘制圆角矩形 + 文字，悬停变亮、按下变暗"""

    def __init__(self, master, text="", command=None, command_right=None,
                 bg=TRACKING_ACCENT_BLUE,
                 fg="#FFFFFF", fnt=("微软雅黑", 9), radius=8,
                 padx=14, pady=7, **kwargs):
        fobj = font.Font(font=fnt)
        self.btn_w = int(fobj.measure(text)) + 2 * padx
        self.btn_h = int(fobj.metrics("linespace")) + 2 * pady
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._bg = bg
        self._hover_bg = tracking_mix(bg, "#FFFFFF", 0.12)
        self._active_bg = tracking_mix(bg, "#000000", 0.15)
        self._text = text
        self._font = fnt
        self._fg = fg
        self._command = command
        self._command_right = command_right
        super().__init__(master, width=self.btn_w, height=self.btn_h,
                         bg=master.cget("bg"), highlightthickness=0,
                         cursor="hand2", **kwargs)
        self._draw(self._bg)
        self.bind("<Enter>", lambda e: self._draw(self._hover_bg))
        self.bind("<Leave>", lambda e: self._draw(self._bg))
        self.bind("<ButtonPress-1>", lambda e: self._draw(self._active_bg))
        self.bind("<ButtonRelease-1>", lambda e: (self._draw(self._hover_bg), self._run()))
        # 右键：触发长计时（仅“开始”键传入 command_right）
        self.bind("<ButtonRelease-3>", lambda e: self._run_right())

    def _draw(self, color):
        self.delete("all")
        w, h = self.btn_w, self.btn_h
        tracking_rounded_rect(self, 1, 1, w - 1, h - 1, self._radius, color)
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg,
                         font=self._font)

    def set_text(self, text):
        """更换文字并自动重算宽度（开始键在开始/重新开始/倒计时中/进行中等状态间切换）"""
        self._text = text
        fobj = font.Font(font=self._font)
        self.btn_w = int(fobj.measure(text)) + 2 * self._padx
        self.config(width=self.btn_w)
        self._draw(self._bg)

    def set_bg(self, color):
        """更换底色，悬停/按下色联动重算"""
        self._bg = color
        self._hover_bg = tracking_mix(color, "#FFFFFF", 0.12)
        self._active_bg = tracking_mix(color, "#000000", 0.15)
        self._draw(self._bg)

    def _run(self):
        if self._command:
            self._command()

    def _run_right(self):
        if self._command_right:
            self._command_right()


class RoundedBar(tk.Canvas):
    """竖向圆角色条按键（无文字）：主界面左右两个任务进入键，悬停变亮"""

    def __init__(self, master, width, height, color, command=None,
                 radius=10, **kwargs):
        super().__init__(master, width=width, height=height,
                         bg=master.cget("bg"), highlightthickness=0,
                         cursor="hand2", **kwargs)
        self.bar_w = width
        self.bar_h = height
        self._radius = min(radius, width // 2)
        self._bg = color
        # 白条悬停变浅灰，彩色条悬停变亮
        if color.strip().lower() == "#ffffff":
            self._hover_bg = tracking_mix(color, "#000000", 0.08)
        else:
            self._hover_bg = tracking_mix(color, "#FFFFFF", 0.15)
        self._command = command
        self._draw(self._bg)
        self.bind("<Enter>", lambda e: self._draw(self._hover_bg))
        self.bind("<Leave>", lambda e: self._draw(self._bg))
        self.bind("<Button-1>", lambda e: self._run())

    def _draw(self, color):
        self.delete("all")
        w, h = self.bar_w, self.bar_h
        tracking_rounded_rect(self, 1, 1, w - 1, h - 1, self._radius, color)

    def _run(self):
        if self._command:
            self._command()


class RoundedPanel(tk.Canvas):
    """圆角卡片容器：圆角背景 + 内嵌 body Frame，供任务显示条/任务行使用"""

    def __init__(self, master, height=40, radius=10, fill="#2c2c2c",
                 padx=8, pady=3, **kwargs):
        super().__init__(master, bg=master.cget("bg"), highlightthickness=0,
                         height=height, **kwargs)
        self._h = height
        self._radius = radius
        self._fill = fill
        self._padx = padx
        self._pady = pady
        self.body = tk.Frame(self, bg=fill)
        self.create_window(0, 0, anchor="nw", window=self.body, tags=("rpanel",))
        self.bind("<Configure>", self._redraw)
        self._redraw()

    def _redraw(self, e=None):
        self.delete("bg")
        w = self.winfo_width() or 200
        h = self._h
        tracking_rounded_rect(self, 1, 1, w - 1, h - 1, self._radius, self._fill,
                              tag="bg")
        self.tag_raise("rpanel")
        self.itemconfig("rpanel", width=w - 2 * self._padx, height=h - 2 * self._pady)
        self.coords("rpanel", self._padx, self._pady)

    def set_fill(self, color):
        """换底色（body 与子控件同步），供阶段主题切换使用"""
        self._fill = color
        self.body.config(bg=color)
        for ch in self.body.winfo_children():
            try:
                ch.config(bg=color)
            except Exception:
                pass
        self._redraw()


class RoundedEntry(tk.Canvas):
    """圆角输入框：Canvas 圆角背景 + 内嵌透明 tk.Entry
    代理 Entry 常用方法（get/delete/insert/bind/config/focus_set），调用方式与 tk.Entry 基本一致
    """

    def __init__(self, master, width=None, height=30, radius=10, padx=12,
                 fnt=("微软雅黑", 11), **entry_kw):
        fill = entry_kw.pop("bg", "#FFFFFF")
        fg = entry_kw.get("fg", "#202124")
        entry_kw.setdefault("insertbackground", fg)
        entry_kw.setdefault("font", fnt)
        # 内嵌 Entry 底色跟随圆角底色，否则默认白底会与浅色文字（如白字）同色而看不见
        entry_kw["bg"] = fill
        super().__init__(master, bg=master.cget("bg"), highlightthickness=0,
                         width=width or 1, height=height)
        self._h = height
        self._radius = radius
        self._fill = fill
        self._padx = padx
        self._min_w = width or 200
        self.entry = tk.Entry(self, relief="flat", borderwidth=0,
                              highlightthickness=0, **entry_kw)
        self.create_window(padx, height // 2, anchor="w", window=self.entry,
                           tags=("rentry",))
        tk.Canvas.bind(self, "<Configure>", self._redraw)
        self._redraw()

    def _redraw(self, e=None):
        tk.Canvas.delete(self, "bg")
        w = self.winfo_width()
        if w <= 2:
            w = self._min_w
        h = self._h
        tracking_rounded_rect(self, 1, 1, w - 1, h - 1, self._radius, self._fill,
                              tag="bg")
        self.tag_raise("rentry")
        self.itemconfig("rentry", width=max(20, w - 2 * self._padx), height=h - 2)
        self.coords("rentry", self._padx, h // 2)

    # ---- 代理内部 Entry 的常用方法 ----
    def get(self):
        return self.entry.get()

    def delete(self, first, last=None):
        return self.entry.delete(first, last)

    def insert(self, index, string):
        return self.entry.insert(index, string)

    def focus_set(self):
        return self.entry.focus_set()

    def bind(self, sequence, func=None, add=None):
        return self.entry.bind(sequence, func, add)

    def config(self, **kw):
        return self.entry.config(**kw)

    def configure(self, **kw):
        return self.entry.config(**kw)


def tracking_calc_progress(obj):
    """计算目标剩余进度：100% - 关键指标平均完成度 × 1.25（可为负数）"""
    krs = obj.get("key_results", [])
    if not krs:
        return 100.0
    avg = sum(float(kr.get("progress", 0)) for kr in krs) / len(krs)
    return round(100.0 - avg * 1.25, 1)


def tracking_parse_date(date_str):
    """把 MM-DD 或 YYYY-MM-DD 字符串转成 date 对象，解析失败返回 None"""
    try:
        s = str(date_str).strip()
        if len(s) == 5 and s[2] == "-":
            s = f"{datetime.now().year}-{s}"
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


class TrackingProgressSlider(tk.Canvas):
    """双层进度滑动条（10% 一档吸附）；计时阶段锁定后不可拖动"""

    def __init__(self, master, width=280, height=14, value=0, step=10,
                 bg_parent=TRACKING_BG_CARD, track_color="#0E244A",
                 saved_color=TRACKING_ACCENT_GREEN, locked=False,
                 on_preview_change=None, **kwargs):
        super().__init__(master, width=width, height=height,
                         bg=bg_parent, highlightthickness=0, **kwargs)
        self.w = width
        self.h = height
        self.step = step
        self.saved_value = value
        self.preview_value = value
        self.track_color = track_color
        self.saved_color = saved_color
        self.preview_color = self._mix(bg_parent, saved_color, 0.2)
        self.radius = height // 2
        self.locked = locked
        self._dragging = False
        self.on_preview_change = on_preview_change
        self._draw()
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _mix(self, bg_hex, fg_hex, alpha):
        bg_hex = bg_hex.lstrip("#")
        fg_hex = fg_hex.lstrip("#")
        br, bg_, bb = [int(bg_hex[i:i + 2], 16) for i in (0, 2, 4)]
        fr, fg_, fb = [int(fg_hex[i:i + 2], 16) for i in (0, 2, 4)]
        r = int(br + (fr - br) * alpha)
        g = int(bg_ + (fg_ - bg_) * alpha)
        b = int(bb + (fb - bb) * alpha)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _snap(self, val):
        return max(0, min(100, round(val / self.step) * self.step))

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        r = self.radius
        self._draw_rounded_bar(0, w, self.track_color)
        saved_w = int(w * self.saved_value / 100)
        preview_w = int(w * self.preview_value / 100)
        if self.preview_value >= self.saved_value:
            if saved_w > 0:
                self._draw_rounded_bar(0, saved_w, self.saved_color)
            if preview_w > saved_w:
                self._draw_rounded_bar(saved_w, preview_w, self.preview_color)
        else:
            if preview_w > 0:
                self._draw_rounded_bar(0, preview_w, self.preview_color)
        knob_x = max(r, min(w - r, preview_w))
        knob_r = r + 3
        self.create_oval(knob_x - knob_r, h // 2 - knob_r,
                         knob_x + knob_r, h // 2 + knob_r,
                         fill="white", outline="white", width=1)

    def _draw_rounded_bar(self, x_start, x_end, color):
        h = self.h
        r = self.radius
        if x_end - x_start < 2 * r:
            cx = (x_start + x_end) // 2
            self.create_oval(cx - r, 0, cx + r, h, fill=color, outline=color)
            return
        self.create_oval(x_start, 0, x_start + 2 * r, h, fill=color, outline=color)
        self.create_rectangle(x_start + r, 0, x_end - r, h, fill=color, outline=color)
        self.create_oval(x_end - 2 * r, 0, x_end, h, fill=color, outline=color)

    def _x_to_value(self, x):
        return self._snap(int(round(x / self.w * 100)))

    def _update_preview(self, val):
        if val != self.preview_value:
            self.preview_value = val
            self._draw()
            if self.on_preview_change:
                self.on_preview_change(val)

    def _on_press(self, e):
        if self.locked:
            return
        self._dragging = True
        self._update_preview(self._x_to_value(e.x))

    def _on_drag(self, e):
        if self.locked or not self._dragging:
            return
        self._update_preview(self._x_to_value(e.x))

    def _on_release(self, e):
        self._dragging = False

    def set_locked(self, locked):
        self.locked = locked

    def is_dirty(self):
        return self.preview_value != self.saved_value

    def save(self):
        self.saved_value = self.preview_value
        self._draw()
        return self.saved_value


class TrackingChartWindow(tk.Toplevel):
    """追踪目标进度曲线图弹窗：理论线 vs 实际进度（每 3 天一个点）"""

    def __init__(self, master, obj):
        super().__init__(master)
        self.title(f"进度图 - {obj['name']}")
        self.configure(bg=TRACKING_BG)
        self.geometry("600x420")
        self.resizable(False, False)
        self.obj = obj
        tk.Label(self, text=obj["name"], font=("微软雅黑", 16, "bold"),
                 bg=TRACKING_BG, fg=TRACKING_TEXT).pack(pady=(15, 10))

        legend = tk.Frame(self, bg=TRACKING_BG)
        legend.pack()
        tk.Frame(legend, width=30, height=2, bg=TRACKING_ACCENT_YELLOW).pack(side="left", padx=(0, 5))
        tk.Label(legend, text="理论时间", font=("微软雅黑", 9), bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="left", padx=(0, 20))
        tk.Frame(legend, width=30, height=2, bg=TRACKING_ACCENT_GREEN).pack(side="left", padx=(0, 5))
        tk.Label(legend, text="实际进度", font=("微软雅黑", 9), bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="left")

        self.canvas_w, self.canvas_h = 520, 300
        self.canvas = tk.Canvas(self, width=self.canvas_w, height=self.canvas_h,
                                bg=TRACKING_CHART_BG, highlightthickness=0)
        self.canvas.pack(pady=10)
        self._draw_chart()

    def _draw_chart(self):
        c = self.canvas
        w, h = self.canvas_w, self.canvas_h
        pad_left, pad_right, pad_top, pad_bottom = 50, 30, 20, 40
        chart_w = w - pad_left - pad_right
        chart_h = h - pad_top - pad_bottom
        y_min, y_max = -25, 100
        y_range = y_max - y_min

        def y_to_pixel(val):
            return pad_top + chart_h * (y_max - val) / y_range

        start_date = tracking_parse_date(self.obj.get("create_time", ""))
        end_date = tracking_parse_date(self.obj.get("deadline", ""))
        today = datetime.now().date()
        if not start_date:
            start_date = today
        if not end_date:
            end_date = start_date + timedelta(days=30)
        total_days = (end_date - start_date).days
        if total_days <= 0:
            total_days = 1

        def x_to_pixel(date):
            return pad_left + chart_w * (date - start_date).days / total_days

        c.create_rectangle(0, 0, w, h, fill=TRACKING_CHART_BG, outline=TRACKING_CHART_BG)
        for val in [100, 75, 50, 25, 0, -25]:
            y = y_to_pixel(val)
            c.create_line(pad_left, y, w - pad_right, y, fill=TRACKING_CHART_GRID, width=1)
            c.create_text(pad_left - 8, y, text=f"{val}", fill=TRACKING_TEXT_SEC,
                          font=("微软雅黑", 8), anchor="e")
        y0 = y_to_pixel(0)
        c.create_line(pad_left, y0, w - pad_right, y0, fill=TRACKING_BORDER, width=2)
        c.create_text(15, pad_top + chart_h / 2, text="进度", fill=TRACKING_TEXT_SEC,
                      font=("微软雅黑", 9), angle=90)
        x_start, x_end = pad_left, w - pad_right
        c.create_line(x_start, pad_top, x_start, h - pad_bottom, fill=TRACKING_BORDER, width=2)
        c.create_line(x_start, h - pad_bottom, x_end, h - pad_bottom, fill=TRACKING_BORDER, width=2)
        c.create_text(x_start, h - pad_bottom + 15, text=start_date.strftime("%m-%d"),
                      fill=TRACKING_TEXT_SEC, font=("微软雅黑", 8), anchor="n")
        c.create_text(x_end, h - pad_bottom + 15, text=end_date.strftime("%m-%d"),
                      fill=TRACKING_TEXT_SEC, font=("微软雅黑", 8), anchor="n")
        c.create_text(w / 2, h - 10, text="时间", fill=TRACKING_TEXT_SEC, font=("微软雅黑", 9))

        c.create_line(x_to_pixel(start_date), y_to_pixel(100),
                      x_to_pixel(end_date), y_to_pixel(0),
                      fill=TRACKING_ACCENT_YELLOW, width=2)

        log = self.obj.get("progress_log", [])
        current_val = tracking_calc_progress(self.obj)
        points_by_date = {}
        for entry in log:
            d = tracking_parse_date(entry.get("date", ""))
            if d:
                points_by_date[d] = entry["value"]
        if today not in points_by_date:
            points_by_date[today] = current_val
        actual_points = sorted(points_by_date.items(), key=lambda p: p[0])
        if len(actual_points) >= 2:
            coords = []
            for d, v in actual_points:
                x = max(x_start, min(x_end, x_to_pixel(d)))
                y = y_to_pixel(v)
                coords.extend([x, y])
            c.create_line(*coords, fill=TRACKING_ACCENT_GREEN, width=2)
        for d, v in actual_points:
            x = max(x_start, min(x_end, x_to_pixel(d)))
            y = y_to_pixel(v)
            c.create_oval(x - 3, y - 3, x + 3, y + 3,
                          fill=TRACKING_ACCENT_GREEN, outline=TRACKING_ACCENT_GREEN)


class TrackingWindow(tk.Toplevel):
    """追踪任务（OKR）界面：目标列表 + 目标详情（KR 进度/进度图）

    - 数据存 okr_data.json（沿用 OKR 实验品格式）
    - 计时阶段由主窗口调用 set_locked(True) 锁定编辑，仅可查看
    """

    def __init__(self, master, on_close=None):
        super().__init__(master)
        self.on_close_cb = on_close
        self.data_file = TRACKING_DATA_FILE
        self.title("追踪任务")
        self.configure(bg=TRACKING_BG)
        self.geometry("720x540")
        self.resizable(False, False)
        self.attributes("-topmost", True)

        self.data = self.load_data()
        self.sweep_progress_logs()
        self.current_index = None
        self.view = "list"
        self.locked = False
        self.kr_sliders = []

        self.build_list()
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ---------- 窗口生命周期 ----------

    def close(self):
        self.destroy()
        if self.on_close_cb:
            self.on_close_cb()

    def set_locked(self, locked):
        """计时阶段锁定编辑（仍可查看）"""
        if self.locked == locked:
            return
        self.locked = locked
        if self.view == "list":
            self.build_list()
        else:
            self.build_detail(self.current_index)

    def clear(self):
        for w in self.winfo_children():
            w.destroy()

    # ---------- 数据 ----------

    def load_data(self):
        """读取 okr_data.json，补齐缺失字段的默认值（含唯一 id，供手机端引用）"""
        return load_tracking_data()

    def save_data(self):
        save_tracking_data(self.data)

    def reload_from_disk(self):
        """手机端改动后重新加载数据并刷新当前视图"""
        self.data = self.load_data()
        if self.view == "detail" and self.current_index is not None \
                and self.current_index < len(self.data):
            self.build_detail(self.current_index)
        else:
            self.build_list()

    def auto_status(self, obj):
        """有 KR 自动转为“正在处理”，否则回到“待办”"""
        if obj["status"] in ("完成", "跳过"):
            return
        obj["status"] = "正在处理" if obj["key_results"] else "待办"

    def maybe_log_progress(self, index):
        """距离上次记录满 3 天时追加一条进度记录（完成/跳过目标不记录）"""
        obj = self.data[index]
        log = obj.get("progress_log", [])
        today = datetime.now().date()
        last_date = None
        for entry in log:
            d = tracking_parse_date(entry.get("date", ""))
            if d and (last_date is None or d > last_date):
                last_date = d
        if last_date and (today - last_date).days < TRACKING_LOG_INTERVAL_DAYS:
            return False
        log.append({"date": today.strftime("%Y-%m-%d"), "value": tracking_calc_progress(obj)})
        obj["progress_log"] = log
        return True

    def sweep_progress_logs(self):
        """打开窗口时对所有进行中的目标补记进度"""
        changed = False
        for i, obj in enumerate(self.data):
            if obj.get("status") in ("完成", "跳过"):
                continue
            if self.maybe_log_progress(i):
                changed = True
        if changed:
            self.save_data()

    # ---------- 列表视图 ----------

    def build_list(self):
        self.clear()
        self.view = "list"
        self.kr_sliders = []
        tk.Label(self, text="我的 OKR 目标", font=("微软雅黑", 18, "bold"),
                 bg=TRACKING_BG, fg=TRACKING_TEXT).pack(pady=(18, 12))

        header = tk.Frame(self, bg=TRACKING_BG)
        header.pack(fill="x", padx=30)
        tk.Label(header, text="目标名称", font=("微软雅黑", 10, "bold"),
                 bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="left")
        tk.Label(header, text="进度", font=("微软雅黑", 10, "bold"),
                 bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="right", padx=(0, 20))
        tk.Label(header, text="状态", font=("微软雅黑", 10, "bold"),
                 bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="right", padx=(0, 10))
        tk.Frame(self, height=1, bg=TRACKING_BORDER).pack(fill="x", padx=30, pady=6)

        list_container = tk.Frame(self, bg=TRACKING_BG)
        list_container.pack(side="top", fill="both", expand=True, padx=30)
        canvas = tk.Canvas(list_container, bg=TRACKING_BG, highlightthickness=0)
        sb = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.list_inner = tk.Frame(canvas, bg=TRACKING_BG)
        self.list_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.list_inner, anchor="nw", tags=("tkw",))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig("tkw", width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.render_list()

        bottom = tk.Frame(self, bg=TRACKING_BG)
        bottom.pack(side="bottom", fill="x", padx=30, pady=12)
        self.lock_hint = tk.Label(bottom, text="", font=("微软雅黑", 9),
                                  bg=TRACKING_BG, fg=TRACKING_ACCENT_YELLOW)
        self.lock_hint.pack(pady=(0, 6))
        self.new_obj_entry = RoundedEntry(bottom, height=34, radius=9,
                                          fnt=("微软雅黑", 11),
                                          bg=TRACKING_BG_INPUT, fg="#202124")
        self.new_obj_entry.pack(side="left", fill="x", expand=True)
        self.new_obj_entry.bind("<Return>", self.add_obj)
        self._apply_list_lock()

    def render_list(self):
        for w in self.list_inner.winfo_children():
            w.destroy()
        for idx, obj in enumerate(self.data):
            panel = RoundedPanel(self.list_inner, height=40, radius=10,
                                 fill=TRACKING_BG_CARD, padx=10, pady=2)
            panel.pack(fill="x", pady=4)
            body = panel.body
            # 固定列结构，保证各行同名列垂直对齐：
            # 0=名称(弹性) 1=删除 2=详情 3=进度 4=状态徽章
            body.grid_columnconfigure(0, weight=1)
            body.grid_rowconfigure(0, weight=1)

            if not self.locked:
                RoundedButton(body, text="✕", fnt=("微软雅黑", 9),
                              bg="#3a3a3a", fg="#ff6666", radius=8,
                              padx=6, pady=4,
                              command=lambda i=idx: self.delete_obj(i)
                              ).grid(row=0, column=1, padx=(0, 6))
            else:
                # 锁定时用等宽占位，保持右侧各列不位移
                tk.Frame(body, width=24, bg=TRACKING_BG_CARD).grid(
                    row=0, column=1, padx=(0, 6))

            RoundedButton(body, text="详情", fnt=("微软雅黑", 9),
                          bg=TRACKING_ACCENT_BLUE, fg="white", radius=8,
                          padx=10, pady=4,
                          command=lambda i=idx: self.build_detail(i)
                          ).grid(row=0, column=2, padx=(0, 8))

            prog = tracking_calc_progress(obj)
            tk.Label(body, text=f"{prog:g}%", font=("微软雅黑", 10, "bold"),
                     bg=TRACKING_BG_CARD, fg=TRACKING_ACCENT_GREEN,
                     width=5, anchor="e").grid(
                row=0, column=3, sticky="e", padx=(0, 10))

            self._status_badge(body, obj["status"]).grid(
                row=0, column=4, sticky="e")

            tk.Label(body, text=obj["name"], font=("微软雅黑", 11),
                     bg=TRACKING_BG_CARD, fg=TRACKING_TEXT, anchor="w"
                     ).grid(row=0, column=0, sticky="w", padx=(2, 8))

    def _status_badge(self, parent, status, width=60, height=24):
        """固定宽度的状态徽章（holder 定宽，防止“待办/正在处理”等文字长短不一导致列错位）"""
        color = TRACKING_STATUS_COLORS.get(status, "#6B7280")
        holder = tk.Frame(parent, width=width, height=height, bg=parent.cget("bg"))
        holder.pack_propagate(False)
        tk.Label(holder, text=status, font=("微软雅黑", 9, "bold"),
                 bg=color, fg="white").pack(fill="both", expand=True)
        return holder

    def _apply_list_lock(self):
        state = "disabled" if self.locked else "normal"
        self.lock_hint.config(text="计时阶段锁定，仅可查看" if self.locked else "")
        self.new_obj_entry.config(state=state)

    def add_obj(self, event=None):
        if self.locked:
            return
        name = self.new_obj_entry.get().strip()
        if not name:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        self.data.append({
            "name": name, "status": "待办", "skip_reason": "",
            "key_results": [], "deadline": "",
            "create_time": today,
            "progress_log": [{"date": today, "value": 100.0}],
        })
        self.new_obj_entry.delete(0, tk.END)
        self.save_data()
        self.render_list()

    def delete_obj(self, index):
        if self.locked:
            return
        if messagebox.askyesno("确认", "确定删除该目标吗？", parent=self):
            self.data.pop(index)
            self.save_data()
            self.render_list()

    # ---------- 详情视图 ----------

    def build_detail(self, index):
        self.clear()
        self.view = "detail"
        self.current_index = index
        self.kr_sliders = []
        obj = self.data[index]

        top = tk.Frame(self, bg=TRACKING_BG)
        top.pack(fill="x", padx=24, pady=(15, 8))
        RoundedButton(top, text="← 返回", fnt=("微软雅黑", 10),
                      bg="#4B5563", fg="white", radius=8, padx=12, pady=5,
                      command=self.build_list).pack(side="left")
        self.name_entry = RoundedEntry(top, height=32, radius=8,
                                       fnt=("微软雅黑", 13, "bold"),
                                       bg=TRACKING_BG_INPUT, fg="#202124")
        self.name_entry.insert(0, obj["name"])
        self.name_entry.pack(side="left", fill="x", expand=True, padx=10)
        RoundedButton(top, text="保存", fnt=("微软雅黑", 10),
                      bg=TRACKING_ACCENT_GREEN, fg="white", radius=8, padx=12, pady=5,
                      command=self.save_goal).pack(side="right")

        status_frame = tk.Frame(self, bg=TRACKING_BG)
        status_frame.pack(fill="x", padx=24, pady=(4, 8))
        tk.Label(status_frame, text="状态：", font=("微软雅黑", 10),
                 bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="left")
        self.status_badge_lbl = self._status_badge(status_frame, obj["status"])
        self.status_badge_lbl.pack(side="left", padx=4)
        if not self.locked:
            if obj["status"] == "正在处理":
                # 右侧操作键统一右边距 0，与“保存”“进度图”右边界对齐
                RoundedButton(status_frame, text="标记完成", fnt=("微软雅黑", 9),
                              bg=TRACKING_ACCENT_GREEN, fg="white", radius=8,
                              padx=10, pady=4,
                              command=self.mark_complete).pack(side="right", padx=(8, 0))
                RoundedButton(status_frame, text="标记跳过", fnt=("微软雅黑", 9),
                              bg=TRACKING_ACCENT_RED, fg="white", radius=8,
                              padx=10, pady=4,
                              command=self.mark_skip).pack(side="right")
            elif obj["status"] in ("完成", "跳过"):
                RoundedButton(status_frame, text="恢复处理", fnt=("微软雅黑", 9),
                              bg=TRACKING_ACCENT_BLUE, fg="white", radius=8,
                              padx=10, pady=4,
                              command=self.restore_processing).pack(side="right")

        dl_row = tk.Frame(self, bg=TRACKING_BG)
        dl_row.pack(fill="x", padx=24, pady=(0, 6))
        tk.Label(dl_row, text="截止日期：", font=("微软雅黑", 10),
                 bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="left")
        self.deadline_entry = RoundedEntry(dl_row, width=90, height=28, radius=8,
                                           fnt=("微软雅黑", 10),
                                           bg=TRACKING_BG_INPUT, fg="#202124")
        self.deadline_entry.insert(0, obj.get("deadline", ""))
        self.deadline_entry.pack(side="left", padx=6)
        RoundedButton(dl_row, text="进度图", fnt=("微软雅黑", 10),
                      bg="#6366F1", fg="white", radius=8, padx=12, pady=5,
                      command=self.show_chart).pack(side="right")

        tk.Frame(self, height=1, bg=TRACKING_BORDER).pack(fill="x", padx=24, pady=4)
        header = tk.Frame(self, bg=TRACKING_BG)
        header.pack(fill="x", padx=24)
        tk.Label(header, text="关键指标", font=("微软雅黑", 10, "bold"),
                 bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="left")
        tk.Label(header, text="完成度", font=("微软雅黑", 10, "bold"),
                 bg=TRACKING_BG, fg=TRACKING_TEXT_SEC).pack(side="right", padx=(0, 8))

        list_container = tk.Frame(self, bg=TRACKING_BG)
        list_container.pack(side="top", fill="both", expand=True, padx=24, pady=6)
        canvas = tk.Canvas(list_container, bg=TRACKING_BG, highlightthickness=0)
        sb = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.kr_inner = tk.Frame(canvas, bg=TRACKING_BG)
        self.kr_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.kr_inner, anchor="nw", tags=("krw",))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig("krw", width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.render_krs()

        bottom = tk.Frame(self, bg=TRACKING_BG)
        bottom.pack(side="bottom", fill="x", padx=24, pady=10)
        self.new_kr_entry = RoundedEntry(bottom, height=32, radius=8,
                                         fnt=("微软雅黑", 10),
                                         bg=TRACKING_BG_INPUT, fg="#202124")
        self.new_kr_entry.pack(side="left", fill="x", expand=True)
        self.new_kr_entry.bind("<Return>", self.add_kr)
        RoundedButton(bottom, text="添加指标", fnt=("微软雅黑", 10),
                      bg=TRACKING_ACCENT_GREEN, fg="white", radius=8, padx=12, pady=5,
                      command=self.add_kr).pack(side="left", padx=(8, 0))
        self._apply_detail_lock()

    def render_krs(self):
        for w in self.kr_inner.winfo_children():
            w.destroy()
        self.kr_sliders = []
        obj = self.data[self.current_index]
        for idx, kr in enumerate(obj["key_results"]):
            card = tk.Frame(self.kr_inner, bg=TRACKING_BG_CARD)
            card.pack(fill="x", pady=5)

            top_row = tk.Frame(card, bg=TRACKING_BG_CARD)
            top_row.pack(fill="x", padx=12, pady=(8, 4))
            tk.Label(top_row, text=kr["name"], font=("微软雅黑", 10),
                     bg=TRACKING_BG_CARD, fg=TRACKING_TEXT, anchor="w").pack(side="left")
            pct = tk.Label(top_row, text=f"{kr['progress']}%", font=("微软雅黑", 10, "bold"),
                           bg=TRACKING_BG_CARD, fg=TRACKING_ACCENT_GREEN)
            pct.pack(side="right", padx=(0, 6))

            slider_row = tk.Frame(card, bg=TRACKING_BG_CARD)
            slider_row.pack(fill="x", padx=12, pady=(0, 8))
            slider = TrackingProgressSlider(
                slider_row, width=300, height=12, value=kr["progress"],
                bg_parent=TRACKING_BG_CARD, locked=self.locked,
                on_preview_change=lambda v, l=pct: l.config(text=f"{v}%"),
            )
            slider.pack(side="left", fill="x", expand=True)
            self.kr_sliders.append(slider)
            if not self.locked:
                RoundedButton(slider_row, text="保存", fnt=("微软雅黑", 9),
                              bg=TRACKING_ACCENT_GREEN, fg="white", radius=8,
                              padx=10, pady=4,
                              command=lambda i=idx, s=slider: self.save_kr(i, s)).pack(side="left", padx=6)
                RoundedButton(slider_row, text="✕", fnt=("微软雅黑", 9),
                              bg="#3a3a3a", fg="#ff6666", radius=8,
                              padx=6, pady=4,
                              command=lambda i=idx: self.delete_kr(i)).pack(side="left", padx=(0, 2))

    def _apply_detail_lock(self):
        state = "disabled" if self.locked else "normal"
        self.name_entry.config(state=state)
        self.deadline_entry.config(state=state)
        self.new_kr_entry.config(state=state)
        for s in self.kr_sliders:
            s.set_locked(self.locked)

    def save_goal(self):
        if self.locked:
            return
        obj = self.data[self.current_index]
        n = self.name_entry.get().strip()
        if n:
            obj["name"] = n
        obj["deadline"] = self.deadline_entry.get().strip()
        self.auto_status(obj)
        self.save_data()
        self.build_detail(self.current_index)

    def add_kr(self, event=None):
        if self.locked:
            return
        name = self.new_kr_entry.get().strip()
        if not name:
            return
        self.data[self.current_index]["key_results"].append({"name": name, "progress": 0})
        self.new_kr_entry.delete(0, tk.END)
        self.save_data()
        self.render_krs()

    def delete_kr(self, index):
        if self.locked:
            return
        self.data[self.current_index]["key_results"].pop(index)
        self.save_data()
        self.render_krs()

    def save_kr(self, index, slider):
        if self.locked:
            return
        if not slider.is_dirty():
            return
        new_val = slider.save()
        self.data[self.current_index]["key_results"][index]["progress"] = new_val
        self.maybe_log_progress(self.current_index)
        self.save_data()
        self.render_krs()

    def mark_complete(self):
        if self.locked:
            return
        obj = self.data[self.current_index]
        obj["status"] = "完成"
        self.maybe_log_progress(self.current_index)
        self.save_data()
        self.build_detail(self.current_index)

    def mark_skip(self):
        if self.locked:
            return
        reason = simpledialog.askstring("跳过理由", "请输入跳过理由：", parent=self)
        if reason is None or not reason.strip():
            return
        obj = self.data[self.current_index]
        obj["status"] = "跳过"
        obj["skip_reason"] = reason.strip()
        self.save_data()
        self.build_detail(self.current_index)

    def restore_processing(self):
        if self.locked:
            return
        obj = self.data[self.current_index]
        obj["status"] = "正在处理" if obj["key_results"] else "待办"
        obj["skip_reason"] = ""
        self.save_data()
        self.build_detail(self.current_index)

    def show_chart(self):
        self.maybe_log_progress(self.current_index)
        self.save_data()
        TrackingChartWindow(self, self.data[self.current_index])


# ==================== 手机端（局域网）Web 服务 ====================
# 纯 Python 标准库实现：桌面端与手机端共享同一批 JSON 文件。
# 手机在同一 WiFi 下用浏览器打开 http://<电脑IP>:8765 即可对两类任务增删改查。

DATA_LOCK = threading.Lock()   # 保护两个 JSON 文件的读写，避免桌面端/手机端同时写入
MOBILE_PORT = 8765
AGILE_TIME_FMT = "%Y-%m-%d %H:%M"
TRACK_STATUSES = ("待办", "正在处理", "完成", "跳过")


def _now_str():
    return datetime.now().strftime(AGILE_TIME_FMT)


def new_agile_task(text):
    """构造一条敏捷任务（含唯一 id 与添加时间）"""
    return {"id": uuid.uuid4().hex[:12], "text": str(text).strip(),
            "created_at": _now_str()}


def _normalize_agile_item(raw):
    """把旧版字符串 / 新版字典统一成标准字典；无法识别返回 None"""
    if isinstance(raw, dict):
        text = str(raw.get("text", "")).strip()
        if not text:
            return None
        item = {"id": str(raw.get("id") or uuid.uuid4().hex[:12]),
                "text": text,
                "created_at": str(raw.get("created_at") or ""),
                "updated_at": int(raw.get("updated_at") or 0)}
        if raw.get("remind_at"):          # 提醒时间（毫秒时间戳），无则不写该字段
            item["remind_at"] = int(raw["remind_at"])
        return item
    if isinstance(raw, str) and raw.strip():
        # 老数据：只有文字，没有时间
        return {"id": uuid.uuid4().hex[:12], "text": raw.strip(),
                "created_at": "", "updated_at": 0}
    return None


# ---------- 同步辅助：删除墓碑 & 修改时间戳 ----------

# 上次落盘时的内容基线（{id: 不含 updated_at 的副本}），用于判断新增/修改/删除
_AGILE_BASELINE = None
_TRACK_BASELINE = None


def _load_tombstones(path):
    """读取删除墓碑列表（[{id, deleted_at}]）"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and d.get("id")]
    except Exception as e:
        print("读取删除记录失败:", e)
    return []


def _save_tombstones(path, lst):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("写入删除记录失败:", e)


def _add_tombstone(path, tid):
    """为被删除的条目记一条墓碑（同 id 只保留最新时间）"""
    lst = _load_tombstones(path)
    now_ms = int(time.time() * 1000)
    for d in lst:
        if d["id"] == tid:
            d["deleted_at"] = now_ms
            break
    else:
        lst.append({"id": tid, "deleted_at": now_ms})
    _save_tombstones(path, lst)


def _strip(item):
    """去掉 updated_at 后的内容副本，用于判断条目是否被改动"""
    return {k: v for k, v in item.items() if k != "updated_at"}


def _diff_and_stamp(items, baseline):
    """
    给新增/内容有变化的条目打上 updated_at=当前毫秒；
    返回 (新的基线, 被删除的 id 列表)
    """
    now_ms = int(time.time() * 1000)
    new_base = {}
    removed = []
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        cur = _strip(it)
        new_base[iid] = cur
        prev = baseline.get(iid) if baseline else None
        if prev is None or prev != cur:
            it["updated_at"] = now_ms          # 新增或内容改变 → 时间戳刷新
        elif "updated_at" not in it:
            it["updated_at"] = 0
    if baseline:
        for iid in baseline:
            if iid not in new_base:
                removed.append(iid)
    return new_base, removed


def _load_agile_unlocked():
    """读取敏捷任务；自动把旧版纯字符串迁移为新结构并写回"""
    global _AGILE_BASELINE
    try:
        if os.path.exists(AGILE_TASKS_FILE):
            with open(AGILE_TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                items = []
                changed = False
                for raw in data:
                    item = _normalize_agile_item(raw)
                    if item is not None:
                        items.append(item)
                    if raw != item:
                        changed = True
                # 先建立基线，再决定是否迁移落盘，避免迁移误判为"内容改动"
                _AGILE_BASELINE = {it["id"]: _strip(it) for it in items if it.get("id")}
                if changed:
                    _save_agile_unlocked(items)  # 迁移后立即落盘，保证 id/时间稳定
                return items
    except Exception as e:
        print("读取 agile_tasks.json 失败:", e)
    return []


def _save_agile_unlocked(tasks):
    global _AGILE_BASELINE
    try:
        base, removed = _diff_and_stamp(tasks, _AGILE_BASELINE)
        _AGILE_BASELINE = base
        for tid in removed:
            _add_tombstone(AGILE_DEL_FILE, tid)   # 记录被删除的条目
        with open(AGILE_TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("写入 agile_tasks.json 失败:", e)


def _normalize_tracking_obj(obj):
    """补齐追踪任务缺失字段的默认值，并确保有唯一 id（返回副本，不修改原对象）"""
    out = dict(obj)
    out.setdefault("id", uuid.uuid4().hex[:12])
    out.setdefault("status", "正在处理" if out.get("key_results") else "待办")
    out.setdefault("skip_reason", "")
    out.setdefault("key_results", [])
    out.setdefault("deadline", "")
    out.setdefault("create_time", datetime.now().strftime("%Y-%m-%d"))
    out.setdefault("progress_log", [])
    out.setdefault("updated_at", 0)
    return out


def _load_tracking_unlocked():
    """读取追踪任务并补齐字段；缺 id 时自动补 id 并写回"""
    global _TRACK_BASELINE
    if os.path.exists(TRACKING_DATA_FILE):
        try:
            with open(TRACKING_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                items = []
                changed = False
                for obj in data:
                    if not isinstance(obj, dict):
                        continue
                    norm = _normalize_tracking_obj(obj)
                    items.append(norm)
                    if norm != obj:
                        changed = True
                # 先建立基线，再决定是否迁移落盘
                _TRACK_BASELINE = {it["id"]: _strip(it) for it in items if it.get("id")}
                if changed:
                    _save_tracking_unlocked(items)  # 迁移后立即落盘
                return items
        except (json.JSONDecodeError, IOError) as e:
            print("读取 okr_data.json 失败:", e)
            return []
    return []


def _save_tracking_unlocked(data):
    global _TRACK_BASELINE
    try:
        base, removed = _diff_and_stamp(data, _TRACK_BASELINE)
        _TRACK_BASELINE = base
        for tid in removed:
            _add_tombstone(TRACKING_DEL_FILE, tid)   # 记录被删除的条目
        with open(TRACKING_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("写入 okr_data.json 失败:", e)


def load_agile_tasks():
    with DATA_LOCK:
        return _load_agile_unlocked()


def save_agile_tasks(tasks):
    with DATA_LOCK:
        _save_agile_unlocked(tasks)


def load_tracking_data():
    with DATA_LOCK:
        return _load_tracking_unlocked()


def save_tracking_data(data):
    with DATA_LOCK:
        _save_tracking_unlocked(data)


def mutate_agile(mutator):
    """加锁下“读取→修改→保存”敏捷任务；mutator(tasks) 的返回值即接口返回"""
    with DATA_LOCK:
        tasks = _load_agile_unlocked()
        result = mutator(tasks)
        _save_agile_unlocked(tasks)
        return result


def mutate_tracking(mutator):
    """加锁下“读取→修改→保存”追踪任务"""
    with DATA_LOCK:
        data = _load_tracking_unlocked()
        result = mutator(data)
        _save_tracking_unlocked(data)
        return result


def _remove_by_id(items, tid):
    """按 id 删除列表中的条目，成功返回 True"""
    for i, it in enumerate(items):
        if it.get("id") == tid:
            items.pop(i)
            return True
    return False


# ---------- 云同步：以 GitHub 仓库当"数据箱" ----------

# 同步配置默认值；实际读取 sync_config.json 覆盖
DEFAULT_SYNC_CFG = {"token": "", "owner": "drmas9", "repo": "yi16",
                    "path": "data/sync.json", "branch": "main"}


def load_sync_config():
    """读取同步配置（sync_config.json）；不存在则返回默认值"""
    cfg = dict(DEFAULT_SYNC_CFG)
    try:
        if os.path.exists(SYNC_CFG_FILE):
            with open(SYNC_CFG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULT_SYNC_CFG:
                    if k in data and data[k] not in (None, ""):
                        cfg[k] = data[k]
    except Exception as e:
        print("读取 sync_config.json 失败:", e)
    return cfg


# ---------- 直连兜底：绕过 hosts 劫持（Steam++ / Watt Toolkit 等）----------
# 公共 DNS 服务器；用它们查真实 IP，不受本机 hosts 影响
_DNS_SERVERS = [("223.5.5.5", 53), ("119.29.29.29", 53), ("1.1.1.1", 53)]


def _dns_query_a(host, timeout=3):
    """用标准库手工构造 DNS 查询包，向公共 DNS 要 A 记录，返回真实 IP 列表"""
    import struct
    import random
    qid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)      # 标准查询
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in host.split(".")) + b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)            # QTYPE=A, QCLASS=IN
    for server, port in _DNS_SERVERS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(timeout)
            s.sendto(packet, (server, port))
            data, _ = s.recvfrom(512)
            s.close()
        except Exception:
            continue
        if len(data) < 12:
            continue
        ancount = struct.unpack(">H", data[6:8])[0]
        idx = 12
        while idx < len(data) and data[idx] != 0:                 # 跳过问题段域名
            idx += 1 + data[idx]
        idx += 5                                                  # 结束字节 + QTYPE + QCLASS
        ips = []
        for _ in range(ancount):
            if idx >= len(data):
                break
            if data[idx] & 0xC0:                                  # 名字用了压缩指针
                idx += 2
            else:
                while idx < len(data) and data[idx] != 0:
                    idx += 1 + data[idx]
                idx += 1
            if idx + 10 > len(data):
                break
            rtype, _, _, rdlen = struct.unpack(">HHIH", data[idx:idx + 10])
            idx += 10
            if rtype == 1 and rdlen == 4:
                ips.append(".".join(str(b) for b in data[idx:idx + 4]))
            idx += rdlen
        if ips:
            return ips
    return []


def _system_ips(host, port=443):
    """系统解析出的地址列表（可能被 hosts 劫持成 127.0.0.1）"""
    ips = []
    try:
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """把 TCP 连接固定到指定 IP，但仍用真实域名做 SNI 与 Host，从而绕过 hosts 劫持"""

    def __init__(self, host, ip, port=443, timeout=20):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _gh_request(url, method="GET", token="", body=None):
    """向 GitHub API 发一个请求，返回 (status, 解析后的 JSON 或 None)。
    先按系统解析的地址连（Steam++ 打开时它指向 127.0.0.1 本地代理，可用）；
    若全部连不上（Steam++ 关闭，本地 443 拒绝），再用公共 DNS 拿真实 IP 直连。
    因此开不开加速器都能连上。"""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or 443
    path = parts.path + (("?" + parts.query) if parts.query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/vnd.github+json",
               "User-Agent": "coldstart-sync"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    def _attempt(ip):
        conn = _PinnedHTTPSConnection(host, ip, port=port, timeout=20)
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        status, reason, resp_headers = resp.status, resp.reason, resp.headers
        conn.close()
        if status >= 400:
            raise urllib.error.HTTPError(url, status, reason, resp_headers, None)
        return status, (json.loads(raw) if raw else None)

    seen, last_err = [], None

    def _try_group(group):
        nonlocal last_err
        for ip in group:
            if ip in seen:
                continue
            seen.append(ip)
            try:
                return True, _attempt(ip)
            except urllib.error.HTTPError:
                raise                                     # 认证/接口错误与 IP 无关，直接抛出
            except Exception as e:
                last_err = e                              # 该 IP 连不上，换下一个
        return False, None

    ok, res = _try_group(_system_ips(host, port))         # 第一组：系统解析（Steam++ 开时即可用）
    if not ok:
        ok, res = _try_group(_dns_query_a(host))          # 第二组：公共 DNS 真实 IP（Steam++ 关时走这条）
    if ok:
        return res
    raise last_err if last_err else urllib.error.URLError("无法连接 " + host)


def _snapshot(items):
    """把列表变成 {id: 不含 updated_at 的内容}，便于比较与合并"""
    out = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("id"):
            out[it["id"]] = _strip(it)
    return out


def _delmap(lst):
    """墓碑列表 → {id: deleted_at}"""
    out = {}
    for d in lst or []:
        if isinstance(d, dict) and d.get("id"):
            out[d["id"]] = int(d.get("deleted_at") or 0)
    return out


def _merge_lists(local_items, remote_items, local_del, remote_del):
    """按 id + updated_at 取最新；再按删除墓碑剔除已删项"""
    merged = {}
    for arr in (local_items, remote_items):
        for it in arr or []:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            ts = int(it.get("updated_at") or 0)
            cur = merged.get(it["id"])
            if cur is None or int(cur.get("updated_at") or 0) < ts:
                merged[it["id"]] = it
    dels = {}
    for arr in (local_del, remote_del):
        for d in arr or []:
            if isinstance(d, dict) and d.get("id"):
                dels[d["id"]] = max(dels.get(d["id"], -1), int(d.get("deleted_at") or 0))
    for iid in list(merged.keys()):
        if iid in dels and int(merged[iid].get("updated_at") or 0) <= dels[iid]:
            del merged[iid]
    return list(merged.values()), [{"id": k, "deleted_at": v} for k, v in dels.items()]


def sync_once():
    """
    同步一次：拉取云端 → 与本地合并 → 写回本地 → （必要时）回推云端。
    返回 True 表示本地数据发生了变化（界面需要刷新）。
    """
    global _AGILE_BASELINE, _TRACK_BASELINE
    cfg = load_sync_config()
    if not cfg.get("token"):
        return False
    base = "https://api.github.com/repos/{}/{}/contents/{}".format(
        cfg["owner"], cfg["repo"], cfg["path"])
    url = base + "?ref=" + urllib.parse.quote(cfg["branch"]) + "&_=" + str(int(time.time() * 1000))

    remote, sha = {}, None
    try:
        _, data = _gh_request(url, "GET", cfg["token"])
        remote = json.loads(base64.b64decode(data.get("content") or "").decode("utf-8")) or {}
        sha = data.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:                 # 404 = 云端还没有文件（首次同步）
            print("云同步读取失败: HTTP", e.code)
            return False
    except Exception as e:
        print("云同步读取失败:", e)
        return False

    remote_a = remote.get("agile") or []
    remote_t = remote.get("track") or []
    remote_da = remote.get("deleted_agile") or []
    remote_dt = remote.get("deleted_track") or []

    with DATA_LOCK:
        agile = _load_agile_unlocked()
        track = _load_tracking_unlocked()
        before_a, before_t = _snapshot(agile), _snapshot(track)

        ma, da = _merge_lists(agile, remote_a, _load_tombstones(AGILE_DEL_FILE), remote_da)
        mt, dt = _merge_lists(track, remote_t, _load_tombstones(TRACKING_DEL_FILE), remote_dt)

        local_changed = (before_a != _snapshot(ma)) or (before_t != _snapshot(mt))

        if local_changed or _delmap(da) != _delmap(_load_tombstones(AGILE_DEL_FILE)) \
                or _delmap(dt) != _delmap(_load_tombstones(TRACKING_DEL_FILE)):
            # 预置基线后再落盘：保留合并结果里的 updated_at，不误触发新时间戳
            _AGILE_BASELINE = _snapshot(ma)
            _TRACK_BASELINE = _snapshot(mt)
            _save_agile_unlocked(ma)
            _save_tracking_unlocked(mt)
        _save_tombstones(AGILE_DEL_FILE, da)
        _save_tombstones(TRACKING_DEL_FILE, dt)

        doc = {"agile": ma, "track": mt, "deleted_agile": da, "deleted_track": dt,
               "version": int(remote.get("version") or 0) + 1,
               "updated_at": int(time.time() * 1000)}

        # 只有和云端确实不同才回推，避免每 30 秒产生一次提交记录
        need_push = (_snapshot(ma) != _snapshot(remote_a) or _snapshot(mt) != _snapshot(remote_t)
                     or _delmap(da) != _delmap(remote_da) or _delmap(dt) != _delmap(remote_dt))

    if need_push:
        try:
            body = {"message": "sync " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "content": base64.b64encode(json.dumps(doc, ensure_ascii=False).encode("utf-8")).decode("ascii"),
                    "branch": cfg["branch"]}
            if sha:
                body["sha"] = sha
            _gh_request(base, "PUT", cfg["token"], body)
        except Exception as e:
            print("云同步写入失败:", e)

    return local_changed


class _MobileHTTPServer(http.server.ThreadingHTTPServer):
    """手机端 HTTP 服务；on_change 用于通知桌面端刷新界面"""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, on_change=None):
        super().__init__(addr, handler)
        self.on_change = on_change

    def notify_change(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as e:
                print("通知桌面刷新失败:", e)


class MobileTaskHandler(http.server.BaseHTTPRequestHandler):
    """处理手机端的增删改查请求（JSON 接口 + 内嵌网页）"""
    server_version = "ColdStartMobile/1.0"

    def log_message(self, fmt, *args):
        pass  # 静音访问日志，避免刷屏

    # ---------- 基础工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _parts(path):
        return path.strip("/").split("/")

    # ---------- 路由 ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, MOBILE_HTML, "text/html; charset=utf-8")
        elif path == "/api/agile":
            self._send_json(200, load_agile_tasks())
        elif path == "/api/tracking":
            self._send_json(200, load_tracking_data())
        else:
            self._send_json(404, {"error": "接口不存在"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._read_body()
        if path == "/api/agile":
            text = str(body.get("text", "")).strip()
            if not text:
                return self._send_json(400, {"error": "任务内容不能为空"})
            item = new_agile_task(text[:15])
            mutate_agile(lambda tasks: tasks.append(item))
            self.server.notify_change()
            return self._send_json(200, item)
        if path == "/api/tracking":
            name = str(body.get("name", "")).strip()
            if not name:
                return self._send_json(400, {"error": "目标名称不能为空"})
            today = datetime.now().strftime("%Y-%m-%d")
            obj = _normalize_tracking_obj({
                "name": name, "status": "待办", "skip_reason": "",
                "key_results": [], "deadline": str(body.get("deadline", "")).strip(),
                "create_time": today,
                "progress_log": [{"date": today, "value": 100.0}],
            })
            mutate_tracking(lambda data: data.append(obj))
            self.server.notify_change()
            return self._send_json(200, obj)
        if path == "/api/remind":
            # 给已有任务设置/清除提醒：{type:'agile'|'track', id, remind_at: 毫秒时间戳(0=清除)}
            kind = str(body.get("type") or "")
            tid = str(body.get("id") or "")
            try:
                ts = int(body.get("remind_at") or 0)
            except (TypeError, ValueError):
                ts = 0

            def set_remind(items):
                for it in items:
                    if it.get("id") == tid:
                        if ts > 0:
                            it["remind_at"] = ts
                        else:
                            it.pop("remind_at", None)
                        return it
                return None

            if kind == "agile":
                found = mutate_agile(set_remind)
            elif kind == "track":
                found = mutate_tracking(set_remind)
            else:
                return self._send_json(400, {"error": "任务类型不正确"})
            if found is None:
                return self._send_json(404, {"error": "未找到该任务"})
            self.server.notify_change()
            return self._send_json(200, found)
        self._send_json(404, {"error": "接口不存在"})

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        parts = self._parts(path)
        body = self._read_body()
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "agile":
            tid = urllib.parse.unquote(parts[2])
            text = str(body.get("text", "")).strip()
            if not text:
                return self._send_json(400, {"error": "任务内容不能为空"})

            def upd(tasks):
                for t in tasks:
                    if t["id"] == tid:
                        t["text"] = text[:15]
                        return t
                return None

            found = mutate_agile(upd)
            if found is None:
                return self._send_json(404, {"error": "未找到该任务"})
            self.server.notify_change()
            return self._send_json(200, found)
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "tracking":
            tid = urllib.parse.unquote(parts[2])

            def upd(data):
                for o in data:
                    if o.get("id") == tid:
                        if str(body.get("name", "")).strip():
                            o["name"] = str(body["name"]).strip()
                        if "status" in body:
                            st = str(body["status"]).strip()
                            if st in TRACK_STATUSES:
                                o["status"] = st
                        if "deadline" in body:
                            o["deadline"] = str(body["deadline"]).strip()
                        if "skip_reason" in body:
                            o["skip_reason"] = str(body["skip_reason"]).strip()
                        return o
                return None

            found = mutate_tracking(upd)
            if found is None:
                return self._send_json(404, {"error": "未找到该任务"})
            self.server.notify_change()
            return self._send_json(200, found)
        self._send_json(404, {"error": "接口不存在"})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        parts = self._parts(path)
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "agile":
            tid = urllib.parse.unquote(parts[2])
            ok = mutate_agile(lambda tasks: _remove_by_id(tasks, tid))
            self.server.notify_change()
            return self._send_json(200, {"ok": bool(ok)})
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "tracking":
            tid = urllib.parse.unquote(parts[2])
            ok = mutate_tracking(lambda data: _remove_by_id(data, tid))
            self.server.notify_change()
            return self._send_json(200, {"ok": bool(ok)})
        self._send_json(404, {"error": "接口不存在"})


# 手机端页面（响应式单页，纯原生 JS，无外部依赖）
MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>冷启动器 · 任务</title>
<style>
:root{--bg:#1b1b1f;--card:#28282e;--card2:#232329;--fg:#f2f2f5;--sec:#9a9aa5;--line:#3a3a42;--green:#34d399;--blue:#4a90ff;--red:#f87171;--gray:#6b7280}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;font-size:15px}
header{position:sticky;top:0;z-index:10;background:var(--bg);padding:12px 14px 8px;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0 0 10px;display:flex;justify-content:space-between;align-items:baseline}
h1 small{font-size:11px;color:var(--sec);font-weight:400}
.tabs{display:flex;gap:8px}
.tab{flex:1;text-align:center;padding:9px 0;border-radius:10px;background:var(--card2);color:var(--sec);border:1px solid transparent}
.tab.on{background:#2f2f38;color:#fff;border-color:var(--line)}
main{padding:12px 14px 96px}
.row{background:var(--card);border-radius:12px;padding:11px 12px;margin-bottom:9px;border:1px solid #33333b}
.top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.text{flex:1;word-break:break-word;line-height:1.35}
.meta{font-size:11px;color:var(--sec);margin-top:6px;display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;color:#fff;white-space:nowrap}
.acts{display:flex;gap:6px;margin-top:9px}
button{font-family:inherit;font-size:13px;border:0;border-radius:9px;padding:7px 14px;background:#3a3a46;color:var(--fg)}
button.p{background:var(--green);color:#08281c;font-weight:600}
button.b{background:var(--blue);color:#fff}
button.d{background:#3a2b2b;color:var(--red)}
input,select{font-family:inherit;font-size:15px;width:100%;padding:9px 10px;border-radius:9px;border:1px solid var(--line);background:#1f1f25;color:var(--fg)}
.edit{margin-top:9px;display:flex;flex-direction:column;gap:8px}
.line{display:flex;gap:8px}
.kr{font-size:12px;color:var(--sec);margin-top:5px;line-height:1.5}
.addbar{position:fixed;left:0;right:0;bottom:0;background:#16161a;border-top:1px solid var(--line);padding:10px 12px;display:flex;gap:8px}
.addbar input{flex:1}
.fab{position:fixed;right:14px;bottom:74px;z-index:12;width:52px;height:52px;border-radius:50%;background:var(--blue);color:#fff;font-size:22px;line-height:52px;text-align:center;box-shadow:0 6px 18px rgba(0,0,0,.45);padding:0}
.sheet{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.55);display:none;align-items:flex-end}
.sheet.on{display:flex}
.sheet .card{width:100%;background:var(--card);border-radius:16px 16px 0 0;padding:16px 14px 20px;display:flex;flex-direction:column;gap:10px;max-height:86vh;overflow:auto}
.sheet h3{margin:0 0 2px;font-size:15px}
.quick{display:flex;gap:6px;flex-wrap:wrap}
.quick button{flex:1;font-size:12px;padding:7px 0;background:#33333d;color:var(--fg);white-space:nowrap}
.hint{font-size:12px;color:var(--sec);line-height:1.4}
.empty{color:var(--sec);text-align:center;padding:44px 0;font-size:13px}
</style>
</head>
<body>
<header>
<h1>冷启动器 · 任务 <small id="stat"></small></h1>
<div class="tabs">
<div class="tab on" id="tabAgile" onclick="switchTab('agile')">敏捷任务</div>
<div class="tab" id="tabTrack" onclick="switchTab('track')">长期任务</div>
</div>
</header>
<main id="list"></main>
<div class="addbar">
<input id="addInput" placeholder="输入任务，回车添加" onkeydown="if(event.key==='Enter')doAdd()">
<button class="p" onclick="doAdd()">添加</button>
</div>
<button class="fab" onclick="openRemind()">⏰</button>
<div id="remindSheet" class="sheet" onclick="if(event.target===this)closeRemind()">
<div class="card">
<h3>设置任务提醒</h3>
<div class="hint">到点后由飞书群机器人推送锁屏提醒</div>
<select id="rmTask" onchange="onRmTaskChange()"></select>
<input id="rmTime" type="datetime-local">
<div class="quick">
<button onclick="quickRemind(60)">1 小时后</button>
<button onclick="quickRemind('tonight')">今晚 20:00</button>
<button onclick="quickRemind('morning')">明早 9:00</button>
</div>
<div class="hint" id="rmNow"></div>
<div class="line">
<button class="p" onclick="saveRemind()">保存提醒</button>
<button onclick="clearRemind()">清除</button>
<button onclick="closeRemind()">取消</button>
</div>
</div>
</div>
<script>
var S={tab:'agile',agile:[],track:[],edit:null};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function api(path,method,body){return fetch(path,{method:method||'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined}).then(function(r){return r.json().then(function(j){if(!r.ok)throw j;return j})})}
function progress(o){var k=o.key_results||[];if(!k.length)return 100;var a=0;for(var i=0;i<k.length;i++){a+=(+k[i].progress||0)}a=a/k.length;return Math.round((100-a*1.25)*10)/10}
var SC={'待办':'#6b7280','正在处理':'#4a90ff','完成':'#34d399','跳过':'#f87171'};
function switchTab(t){S.tab=t;S.edit=null;document.getElementById('tabAgile').className='tab'+(t==='agile'?' on':'');document.getElementById('tabTrack').className='tab'+(t==='track'?' on':'');document.getElementById('addInput').placeholder=(t==='agile'?'输入敏捷任务，回车添加':'输入长期目标，回车添加');render()}
function load(){return Promise.all([api('/api/agile').then(function(d){S.agile=d}),api('/api/tracking').then(function(d){S.track=d})])}
function render(){
  var el=document.getElementById('list'),html='';
  if(S.tab==='agile'){
    if(!S.agile.length){html='<div class="empty">还没有敏捷任务</div>'}
    S.agile.forEach(function(t){
      var ed=S.edit&&S.edit.type==='agile'&&S.edit.id===t.id;
      html+='<div class="row">';
      if(ed){
        html+='<div class="edit"><input id="ei" maxlength="15" value="'+esc(t.text)+'">'
            +'<div class="line"><button class="p" onclick="saveAgile(\''+t.id+'\')">保存</button>'
            +'<button onclick="cancelEdit()">取消</button></div></div>';
      }else{
        html+='<div class="top"><div class="text">'+esc(t.text)+'</div></div>'
            +'<div class="meta"><span>🕒 添加时间 '+esc(t.created_at||'无记录')+'</span>'
            +(t.remind_at?'<span>⏰ '+_fmtTs(t.remind_at)+'</span>':'')
            +'</div>'
            +'<div class="acts"><button class="b" onclick="editAgile(\''+t.id+'\')">编辑</button>'
            +'<button class="d" onclick="delAgile(\''+t.id+'\')">删除</button></div>';
      }
      html+='</div>';
    });
  }else{
    if(!S.track.length){html='<div class="empty">还没有长期任务</div>'}
    S.track.forEach(function(o){
      var ed=S.edit&&S.edit.type==='track'&&S.edit.id===o.id;
      html+='<div class="row">';
      if(ed){
        var opts=['待办','正在处理','完成','跳过'].map(function(s){return '<option'+(s===o.status?' selected':'')+'>'+s+'</option>'}).join('');
        html+='<div class="edit"><input id="ei" value="'+esc(o.name)+'">'
            +'<div class="line"><select id="es">'+opts+'</select></div>'
            +'<input id="ed" placeholder="截止日期（可留空）" value="'+esc(o.deadline||'')+'">'
            +'<div class="line"><button class="p" onclick="saveTrack(\''+o.id+'\')">保存</button>'
            +'<button onclick="cancelEdit()">取消</button></div></div>';
      }else{
        html+='<div class="top"><div class="text">'+esc(o.name)+'</div>'
            +'<span class="badge" style="background:'+(SC[o.status]||'#6b7280')+'">'+esc(o.status)+'</span></div>'
            +'<div class="meta"><span>🕒 创建 '+esc(o.create_time||'')+'</span>'
            +(o.deadline?'<span>📅 '+esc(o.deadline)+'</span>':'')
            +(o.remind_at?'<span>⏰ '+_fmtTs(o.remind_at)+'</span>':'')
            +'<span>进度 '+progress(o)+'%</span></div>';
        var k=o.key_results||[];
        if(k.length){html+='<div class="kr">关键指标：'+k.map(function(x){return esc(x.name)+' '+x.progress+'%'}).join('、')+'</div>'}
        html+='<div class="acts"><button class="b" onclick="editTrack(\''+o.id+'\')">编辑</button>'
            +'<button class="d" onclick="delTrack(\''+o.id+'\')">删除</button></div>';
      }
      html+='</div>';
    });
  }
  el.innerHTML=html;
  document.getElementById('stat').textContent='敏捷 '+S.agile.length+' · 长期 '+S.track.length;
  if(S.edit){setTimeout(function(){var x=document.getElementById('ei');if(x){x.focus()}},0)}
}
function doAdd(){
  var inp=document.getElementById('addInput'),v=inp.value.trim();
  if(!v)return;
  var p=S.tab==='agile'?api('/api/agile','POST',{text:v}):api('/api/tracking','POST',{name:v});
  p.then(function(){inp.value='';return load()}).then(render).catch(function(e){alert((e&&e.error)||'添加失败')});
}
function editAgile(id){S.edit={type:'agile',id:id};render()}
function editTrack(id){S.edit={type:'track',id:id};render()}
function cancelEdit(){S.edit=null;render()}
function saveAgile(id){var v=document.getElementById('ei').value.trim();if(!v)return;
  api('/api/agile/'+encodeURIComponent(id),'PUT',{text:v}).then(function(){S.edit=null;return load()}).then(render).catch(function(e){alert((e&&e.error)||'保存失败')})}
function saveTrack(id){var v=document.getElementById('ei').value.trim();if(!v){alert('名称不能为空');return}
  var st=document.getElementById('es').value,dl=document.getElementById('ed').value.trim();
  api('/api/tracking/'+encodeURIComponent(id),'PUT',{name:v,status:st,deadline:dl}).then(function(){S.edit=null;return load()}).then(render).catch(function(e){alert((e&&e.error)||'保存失败')})}
function delAgile(id){api('/api/agile/'+encodeURIComponent(id),'DELETE').then(load).then(render).catch(function(e){alert((e&&e.error)||'删除失败')})}
function delTrack(id){api('/api/tracking/'+encodeURIComponent(id),'DELETE').then(load).then(render).catch(function(e){alert((e&&e.error)||'删除失败')})}
function refresh(){load().then(render).catch(function(){})}
/* ===== 提醒：浮球 → 选任务 → 选时间 ===== */
function _pad2(n){return (n<10?'0':'')+n}
function _fmtTs(ts){var d=new Date(+ts);return d.getFullYear()+'-'+_pad2(d.getMonth()+1)+'-'+_pad2(d.getDate())+' '+_pad2(d.getHours())+':'+_pad2(d.getMinutes())}
function _toLocalInput(d){return d.getFullYear()+'-'+_pad2(d.getMonth()+1)+'-'+_pad2(d.getDate())+'T'+_pad2(d.getHours())+':'+_pad2(d.getMinutes())}
function _rmSelected(){
  var v=document.getElementById('rmTask').value||'',i=v.indexOf(':');
  return i<0?null:{type:v.slice(0,i),id:v.slice(i+1)};
}
function _rmFind(sel){
  if(!sel)return null;
  var arr=sel.type==='agile'?S.agile:S.track;
  for(var i=0;i<arr.length;i++)if(arr[i].id===sel.id)return arr[i];
  return null;
}
function openRemind(){
  var html='';
  S.agile.forEach(function(t){html+='<option value="agile:'+t.id+'">[敏捷] '+esc(t.text)+'</option>'});
  S.track.forEach(function(o){html+='<option value="track:'+o.id+'">[长期] '+esc(o.name)+'</option>'});
  if(!html)html='<option value="">（暂无任务，请先添加）</option>';
  var sel=document.getElementById('rmTask');sel.innerHTML=html;
  var want=S.tab+':'+((S.tab==='agile'?S.agile[0]:S.track[0])||{id:''}).id;
  for(var i=0;i<sel.options.length;i++)if(sel.options[i].value===want){sel.selectedIndex=i;break}
  document.getElementById('rmNow').textContent='当前时间 '+_fmtTs(Date.now());
  onRmTaskChange();
  document.getElementById('remindSheet').className='sheet on';
}
function onRmTaskChange(){
  var t=_rmFind(_rmSelected());
  document.getElementById('rmTime').value=_toLocalInput(new Date(t&&t.remind_at?+t.remind_at:Date.now()+3600000));
}
function quickRemind(k){
  var d=new Date();
  if(k===60)d=new Date(Date.now()+3600000);
  else if(k==='tonight'){d.setHours(20,0,0,0);if(d.getTime()<=Date.now())d.setDate(d.getDate()+1)}
  else{d.setDate(d.getDate()+1);d.setHours(9,0,0,0)}
  document.getElementById('rmTime').value=_toLocalInput(d);
}
function closeRemind(){document.getElementById('remindSheet').className='sheet'}
function _setRemind(ts){
  var sel=_rmSelected();
  if(!sel||!sel.id){alert('请先选择任务');return}
  api('/api/remind','POST',{type:sel.type,id:sel.id,remind_at:ts}).then(function(){
    closeRemind();return load();
  }).then(render).catch(function(e){alert((e&&e.error)||'设置提醒失败')});
}
function saveRemind(){
  var v=document.getElementById('rmTime').value;
  if(!v){alert('请选择提醒时间');return}
  var ts=new Date(v).getTime();
  if(!(ts>0)){alert('提醒时间不正确');return}
  if(ts<=Date.now()&&!confirm('这个时间已经过去了，仍然保存吗？'))return;
  _setRemind(ts);
}
function clearRemind(){_setRemind(0)}
load().then(render);
setInterval(function(){if(!S.edit&&document.visibilityState==='visible')refresh()},6000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    root = tk.Tk()
    app = CountdownClock(root)
    root.mainloop()