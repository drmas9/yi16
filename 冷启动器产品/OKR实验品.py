import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import copy
import json
import os
import sys
from datetime import datetime, timedelta

# ===== 数据文件路径 =====
DATA_DIR = r"G:\冷启动器产品"
DATA_FILE = os.path.join(DATA_DIR, "okr_data.json")

# 配色方案（深蓝主题）
BG_DARK = "#0F2B5B"
BG_CARD = "#1A3A6B"
BG_INPUT = "#FFFFFF"
TEXT_PRIMARY = "#FFFFFF"
TEXT_SECOND = "#8BA4CC"
ACCENT_BLUE = "#4A90FF"
ACCENT_GREEN = "#34D399"
ACCENT_RED = "#F87171"
ACCENT_YELLOW = "#FBBF24"
BORDER_COLOR = "#2A4A7A"
CHART_BG = "#0A1F44"
CHART_GRID = "#1E3A6B"

STATUS_COLORS = {
    "待办": "#6B7280",
    "正在处理": ACCENT_BLUE,
    "完成": ACCENT_GREEN,
    "跳过": ACCENT_RED
}

# 滑动条步长（10% 一档）
SLIDER_STEP = 10
# 进度记录间隔（天）
LOG_INTERVAL_DAYS = 3
# 当前年份
CURRENT_YEAR = 2026


def calc_progress(obj):
    """计算目标的剩余进度（百分数）。

    公式：100% - 关键指标(KR)完成度的平均数 × 1.25
    - 没有任何 KR 时，视为平均完成度 0，结果为 100
    - 所有 KR 都完成（平均 100%）时，结果为 100 - 125 = -25（允许为负数）
    """
    krs = obj.get("key_results", [])
    if not krs:
        return 100.0
    avg = sum(float(kr.get("progress", 0)) for kr in krs) / len(krs)
    return round(100.0 - avg * 1.25, 1)


def parse_date(date_str):
    """把 MM-DD 或 YYYY-MM-DD 字符串转成 date 对象，解析失败返回 None"""
    try:
        s = str(date_str).strip()
        if len(s) == 5 and s[2] == "-":  # MM-DD 补当前年份
            s = f"{CURRENT_YEAR}-{s}"
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def setup_autostart(app_name="OKR管理器"):
    """把本程序写入当前用户的注册表启动项，实现开机自启动。

    - 写入 HKEY_CURRENT_USER\\...\\Run，无需管理员权限
    - 优先用 pythonw.exe（开机时不弹出黑色控制台窗口），找不到再退回 python.exe
    - 中断/失败只静默跳过，不影响程序正常使用
    """
    try:
        import winreg
    except ImportError:
        return

    # 启动命令：解释器 + 本脚本绝对路径
    exe_dir = os.path.dirname(sys.executable)
    exe = os.path.join(exe_dir, "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    cmd = f'"{exe}" "{os.path.abspath(__file__)}"'

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
    except Exception:
        pass  # 写注册表失败不阻断程序


class RoundedButton(tk.Canvas):
    """圆角按钮 — 用 Canvas 绘制，自带 hover 效果"""

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
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw_button(self, color):
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
        hex_color = hex_color.lstrip("#")
        r, g, b = [int(hex_color[i:i + 2], 16) for i in (0, 2, 4)]
        r = min(255, r + amount)
        g = min(255, g + amount)
        b = min(255, b + amount)
        return f"#{r:02x}{g:02x}{b:02x}"


class StatusBadge(tk.Canvas):
    """圆角状态标签（胶囊形）"""

    def __init__(self, master, status, width=75, height=26, bg_parent=BG_DARK, **kwargs):
        color = STATUS_COLORS.get(status, "#6B7280")
        super().__init__(master, width=width, height=height,
                         bg=bg_parent, highlightthickness=0, **kwargs)
        self.status = status
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
        self.create_text(w / 2, h / 2, text=self.status,
                         fill="white", font=("微软雅黑", 9, "bold"))

    def set_status(self, status):
        self.status = status
        self.color = STATUS_COLORS.get(status, "#6B7280")
        self._draw()


class ProgressSlider(tk.Canvas):
    """双层进度滑动条 — 10% 一档吸附"""

    def __init__(self, master, width=300, height=14, value=0, step=SLIDER_STEP,
                 bg_parent=BG_CARD, track_color="#0E244A",
                 saved_color=ACCENT_GREEN,
                 on_preview_change=None,
                 **kwargs):
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
        raw = int(round(x / self.w * 100))
        return self._snap(raw)

    def _update_preview(self, val):
        if val != self.preview_value:
            self.preview_value = val
            self._draw()
            if self.on_preview_change:
                self.on_preview_change(val)

    def _on_press(self, e):
        self._dragging = True
        self._update_preview(self._x_to_value(e.x))

    def _on_drag(self, e):
        if not self._dragging:
            return
        self._update_preview(self._x_to_value(e.x))

    def _on_release(self, e):
        self._dragging = False

    def get_preview_value(self):
        return self.preview_value

    def get_saved_value(self):
        return self.saved_value

    def is_dirty(self):
        return self.preview_value != self.saved_value

    def save(self):
        self.saved_value = self.preview_value
        self._draw()
        return self.saved_value

    def reset(self):
        self.preview_value = self.saved_value
        self._draw()
        return self.saved_value

    def set_value(self, value):
        value = self._snap(int(value))
        self.saved_value = value
        self.preview_value = value
        self._draw()


# ================================================================
#  进度图窗口
# ================================================================
class ProgressChartWindow(tk.Toplevel):
    """跟踪消耗曲线图弹窗"""

    def __init__(self, master, obj):
        super().__init__(master)
        self.title(f"进度图 - {obj['name']}")
        self.configure(bg=BG_DARK)
        self.geometry("600x420")
        self.resizable(False, False)

        self.obj = obj

        tk.Label(self, text=obj["name"], font=("微软雅黑", 16, "bold"),
                 bg=BG_DARK, fg=TEXT_PRIMARY).pack(pady=(15, 10))

        # 图例
        legend = tk.Frame(self, bg=BG_DARK)
        legend.pack()
        tk.Frame(legend, width=30, height=2, bg=ACCENT_YELLOW).pack(side="left", padx=(0, 5))
        tk.Label(legend, text="理论时间", font=("微软雅黑", 9),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="left", padx=(0, 20))
        tk.Frame(legend, width=30, height=2, bg=ACCENT_GREEN).pack(side="left", padx=(0, 5))
        tk.Label(legend, text="实际进度", font=("微软雅黑", 9),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="left")

        # 画布
        self.canvas_w = 520
        self.canvas_h = 300
        self.canvas = tk.Canvas(self, width=self.canvas_w, height=self.canvas_h,
                                bg=CHART_BG, highlightthickness=0)
        self.canvas.pack(pady=10)

        self._draw_chart()

    def _parse_date(self, date_str):
        """把 MM-DD 或 YYYY-MM-DD 转成 date 对象"""
        return parse_date(date_str)

    def _calc_progress(self, obj):
        """剩余进度：100% - 关键指标平均数 × 1.25（可为负数）"""
        return calc_progress(obj)

    def _draw_chart(self):
        c = self.canvas
        w, h = self.canvas_w, self.canvas_h
        pad_left = 50
        pad_right = 30
        pad_top = 20
        pad_bottom = 40
        chart_w = w - pad_left - pad_right
        chart_h = h - pad_top - pad_bottom

        # 纵轴范围：-25 到 100
        y_min, y_max = -25, 100
        y_range = y_max - y_min  # 125

        def y_to_pixel(val):
            return pad_top + chart_h * (y_max - val) / y_range

        # 时间范围
        start_date = self._parse_date(self.obj.get("create_time", ""))
        end_date = self._parse_date(self.obj.get("deadline", ""))
        today = datetime.now().date()

        if not start_date:
            start_date = today
        if not end_date:
            end_date = start_date + timedelta(days=30)

        total_days = (end_date - start_date).days
        if total_days <= 0:
            total_days = 1

        def x_to_pixel(date):
            days = (date - start_date).days
            return pad_left + chart_w * days / total_days

        # 背景
        c.create_rectangle(0, 0, w, h, fill=CHART_BG, outline=CHART_BG)

        # 横向网格线 + 纵轴刻度（100, 75, 50, 25, 0, -25）
        for val in [100, 75, 50, 25, 0, -25]:
            y = y_to_pixel(val)
            c.create_line(pad_left, y, w - pad_right, y,
                          fill=CHART_GRID, width=1)
            c.create_text(pad_left - 8, y, text=f"{val}",
                          fill=TEXT_SECOND, font=("微软雅黑", 8),
                          anchor="e")

        # 0 轴加粗
        y0 = y_to_pixel(0)
        c.create_line(pad_left, y0, w - pad_right, y0,
                      fill=BORDER_COLOR, width=2)

        # 纵轴标签
        c.create_text(15, pad_top + chart_h / 2, text="进度",
                      fill=TEXT_SECOND, font=("微软雅黑", 9), angle=90)

        # 横轴：起止日期
        x_start = pad_left
        x_end = w - pad_right
        c.create_line(x_start, pad_top, x_start, h - pad_bottom,
                      fill=BORDER_COLOR, width=2)
        c.create_line(x_start, h - pad_bottom, x_end, h - pad_bottom,
                      fill=BORDER_COLOR, width=2)

        c.create_text(x_start, h - pad_bottom + 15,
                      text=start_date.strftime("%m-%d"),
                      fill=TEXT_SECOND, font=("微软雅黑", 8), anchor="n")
        c.create_text(x_end, h - pad_bottom + 15,
                      text=end_date.strftime("%m-%d"),
                      fill=TEXT_SECOND, font=("微软雅黑", 8), anchor="n")
        c.create_text(w / 2, h - 10, text="时间",
                      fill=TEXT_SECOND, font=("微软雅黑", 9))

        # 理论线：开始(100%) → 结束(0%)
        c.create_line(x_to_pixel(start_date), y_to_pixel(100),
                      x_to_pixel(end_date), y_to_pixel(0),
                      fill=ACCENT_YELLOW, width=2)

        # 实际进度线
        log = self.obj.get("progress_log", [])
        current_val = self._calc_progress(self.obj)

        # 组装实际点：历史记录 + 当前值
        points_by_date = {}
        for entry in log:
            d = self._parse_date(entry.get("date", ""))
            if d:
                points_by_date[d] = entry["value"]

        # 当前实时值（若今天还没有记录点，追加一个，保证曲线延伸到今天）
        if today not in points_by_date:
            points_by_date[today] = current_val

        # 按日期升序排列，每 3 天一个点、依次连成折线
        actual_points = sorted(points_by_date.items(), key=lambda p: p[0])

        # 画实际线（采样点之间用直线段连接，不做平滑以免失真）
        if len(actual_points) >= 2:
            coords = []
            for d, v in actual_points:
                x = max(x_start, min(x_end, x_to_pixel(d)))
                y = y_to_pixel(v)
                coords.extend([x, y])
            c.create_line(*coords, fill=ACCENT_GREEN, width=2)

        # 画数据点
        for d, v in actual_points:
            x = max(x_start, min(x_end, x_to_pixel(d)))
            y = y_to_pixel(v)
            c.create_oval(x - 3, y - 3, x + 3, y + 3,
                          fill=ACCENT_GREEN, outline=ACCENT_GREEN)


# ================================================================
#  收纳球
# ================================================================
# 键控透明色：设置-透明后窗口区域内该颜色的像素会变透明，只露出圆形
COLOR_TRANSPARENT = "#FF00FF"


class BallWindow(tk.Toplevel):
    """左上角收纳球：点击球打开主窗口；主窗口最小化时回到球。

    - 无边框、置顶、可自由拖动
    - 左键点击：打开主窗口（隐藏球）
    - 右键菜单：打开 / 退出
    """

    def __init__(self, master, on_click, on_exit, size=64):
        super().__init__(master)
        self.on_click = on_click
        self.on_exit = on_exit
        self.size = size

        # 无边框 + 置顶 + 键控透明只露出圆形
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.config(bg=COLOR_TRANSPARENT)
        self.attributes("-transparentcolor", COLOR_TRANSPARENT)

        # 初始位置：屏幕左上角
        self.geometry(f"{size}x{size}+14+14")

        self.canvas = tk.Canvas(self, width=size, height=size,
                                bg=COLOR_TRANSPARENT, highlightthickness=0)
        self.canvas.pack()
        self._draw()

        # 交互：左键 = 拖拽/点击，右键 = 菜单
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._show_menu)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="打开", command=self.on_click)
        self.menu.add_command(label="退出", command=self.on_exit)

        self.drag_start = None

        # 默认先隐藏，由外部控制何时显示
        self.withdraw()

    def _draw(self):
        """绘制一个带高光的蓝色圆球，中间写 OKR"""
        c = self.canvas
        s = self.size
        pad = 4
        r = s - pad  # 球直径
        # 底部阴影
        c.create_oval(pad, pad + 3, r, r + 3, fill="#082142", outline="")
        # 球体
        c.create_oval(pad, pad, r, r, fill=ACCENT_BLUE,
                      outline="#7FB2FF", width=2)
        # 左上高光
        c.create_oval(pad + 6, pad + 5, r - 20, r - 22,
                      fill="#7FB2FF", outline="")
        # 文字
        c.create_text(s / 2, s / 2 - 1, text="OKR", fill="white",
                      font=("微软雅黑", 9, "bold"))

    # ---- 拖拽 & 点击 ----
    def _on_press(self, e):
        self.drag_start = (e.x_root, e.y_root, self.winfo_x(), self.winfo_y())

    def _on_drag(self, e):
        if not self.drag_start:
            return
        sx, sy, wx, wy = self.drag_start
        self.geometry(f"+{wx + e.x_root - sx}+{wy + e.y_root - sy}")

    def _on_click(self, e):
        # 松开时若无明显位移视为点击
        if self.drag_start:
            sx, sy, _, _ = self.drag_start
            if abs(e.x_root - sx) < 4 and abs(e.y_root - sy) < 4:
                self.on_click()
        self.drag_start = None

    def _show_menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    # ---- 显示/隐藏 ----
    def show(self):
        self.deiconify()
        self.lift()

    def hide(self):
        self.withdraw()


class OKRApp:
    def __init__(self, root):
        self.root = root
        self.root.title("OKR 管理器")
        self.root.geometry("700x560")
        self.root.configure(bg=BG_DARK)

        # 确保数据目录存在
        os.makedirs(DATA_DIR, exist_ok=True)

        self.data = self.load_data()
        # 启动时自动补记进度点：距上一个点满 3 天的目标记一个今天的值
        self.sweep_progress_logs()
        self.current_view = "main"
        self.current_obj_index = None
        self.edit_mode = False
        self.edit_buffer = None

        self.build_main()

        # 开机自启动：把本程序写入当前用户注册表启动项
        setup_autostart()

        # 收纳球：启动时先以球的形式停在左上角，主窗口隐藏，点击球再打开
        self.ball = BallWindow(root, on_click=self.open_main, on_exit=self.on_close)
        # 拦截最小化：按最小化按钮时隐藏主窗口并唤出收纳球
        self.root.bind("<Unmap>", self._on_root_unmap)

        # 窗口关闭时自动保存
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 首次打开统一以收纳球形式出现
        self.ball.show()
        self.root.withdraw()

    # ---------- 收纳球联动 ----------
    def open_main(self):
        """点击收纳球 → 显示主窗口（隐藏球）"""
        self.ball.hide()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_root_unmap(self, event):
        """主窗口最小化（或隐藏）时，收起主窗口、唤出收纳球

        withdraw() 也会触发 <Unmap>，但此时状态是 withdrawn 而非 iconic，
        借此避免重复处理。
        """
        if self.root.state() == "iconic":
            self.root.withdraw()
            self.ball.show()

    # ---------- 数据 ----------
    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for obj in data:
                    if "status" not in obj:
                        obj["status"] = "正在处理" if obj.get("key_results") else "待办"
                    if "skip_reason" not in obj:
                        obj["skip_reason"] = ""
                    if "key_results" not in obj:
                        obj["key_results"] = []
                    # 新增字段兼容
                    if "deadline" not in obj:
                        obj["deadline"] = ""
                    if "create_time" not in obj:
                        obj["create_time"] = datetime.now().strftime("%Y-%m-%d")
                    if "progress_log" not in obj:
                        obj["progress_log"] = []
                return data
            except (json.JSONDecodeError, IOError):
                return []
        return []

    def save_data(self):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def on_close(self):
        try:
            if self.edit_mode and self.edit_buffer is not None:
                if self.current_view == "main":
                    for idx, name_var in enumerate(getattr(self, 'name_vars', [])):
                        if idx < len(self.edit_buffer):
                            n = name_var.get().strip()
                            if n:
                                self.edit_buffer[idx]["name"] = n
                    # 截止日期
                    for idx, dl_var in enumerate(getattr(self, 'deadline_vars', [])):
                        if idx < len(self.edit_buffer):
                            self.edit_buffer[idx]["deadline"] = dl_var.get().strip()
                    for obj in self.edit_buffer:
                        self.auto_status(obj)
                    self.data = self.edit_buffer
                elif self.current_view == "detail" and self.current_obj_index is not None:
                    for idx, name_var in enumerate(getattr(self, 'kr_name_vars', [])):
                        if idx < len(self.edit_buffer["key_results"]):
                            n = name_var.get().strip()
                            if n:
                                self.edit_buffer["key_results"][idx]["name"] = n
                    for idx, prog_var in enumerate(getattr(self, 'kr_prog_vars', [])):
                        if idx < len(self.edit_buffer["key_results"]):
                            try:
                                num = int(prog_var.get().strip())
                                num = max(0, min(100, num))
                                self.edit_buffer["key_results"][idx]["progress"] = num
                            except ValueError:
                                pass
                    self.auto_status(self.edit_buffer)
                    self.data[self.current_obj_index] = self.edit_buffer
            self.save_data()
        except Exception as e:
            messagebox.showerror("保存失败", f"退出时保存数据出错：{e}")
        finally:
            self.root.destroy()

    def auto_status(self, obj):
        if obj["status"] in ("完成", "跳过"):
            return
        obj["status"] = "正在处理" if obj["key_results"] else "待办"

    # ===== 进度记录 =====
    def maybe_log_progress(self, obj_index):
        """距上次记录满 LOG_INTERVAL_DAYS 天（或从未记录）时，追加一个今天的点。

        过去几天的 KR 数值无法还原，所以即使隔了很多天也只补“今天”这一个点，
        之后每 3 天会持续形成新点。返回 True 表示本次新增了记录。
        """
        obj = self.data[obj_index]
        log = obj.get("progress_log", [])
        today = datetime.now().date()

        # 找已有记录中最晚的日期（不依赖列表顺序）
        last_date = None
        for entry in log:
            d = parse_date(entry.get("date", ""))
            if d and (last_date is None or d > last_date):
                last_date = d

        # 距上次记录不足 3 天则不记
        if last_date and (today - last_date).days < LOG_INTERVAL_DAYS:
            return False

        log.append({
            "date": today.strftime("%Y-%m-%d"),
            "value": calc_progress(obj)
        })
        obj["progress_log"] = log
        return True

    def sweep_progress_logs(self):
        """扫描所有未结束的目标，把满 3 天到期的进度点统一记上并落盘。

        已完成/已跳过的目标视为冻结，不再追加；这样即使不手动拖动保存，
        只要打开软件，曲线上也能保证每 3 天出现一个新点。
        """
        changed = False
        for i, obj in enumerate(self.data):
            if obj.get("status") in ("完成", "跳过"):
                continue
            if self.maybe_log_progress(i):
                changed = True
        if changed:
            self.save_data()

    def show_progress_chart(self, index):
        """显示进度图弹窗（打开前先检查是否到了 3 天记点时间）"""
        obj = self.data[index]
        if obj.get("status") not in ("完成", "跳过") and \
                self.maybe_log_progress(index):
            self.save_data()
        ProgressChartWindow(self.root, self.data[index])

    def clear(self):
        for w in self.root.winfo_children():
            w.destroy()

    # ================================================================
    #  主界面
    # ================================================================
    def build_main(self):
        self.clear()
        self.current_view = "main"

        tk.Label(self.root, text="我的 OKR 目标", font=("微软雅黑", 20, "bold"),
                 bg=BG_DARK, fg=TEXT_PRIMARY).pack(pady=(25, 20))

        header = tk.Frame(self.root, bg=BG_DARK)
        header.pack(fill="x", padx=40)
        tk.Label(header, text="目标名称", font=("微软雅黑", 11, "bold"),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="left")
        tk.Label(header, text="状态", font=("微软雅黑", 11, "bold"),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="right", padx=(0, 160))

        tk.Frame(self.root, height=1, bg=BORDER_COLOR).pack(fill="x", padx=40, pady=(8, 8))

        bottom_h = 180 if self.edit_mode else 100
        bottom_frame = tk.Frame(self.root, bg=BG_DARK, height=bottom_h)
        bottom_frame.pack(side="bottom", fill="x")
        bottom_frame.pack_propagate(False)
        self._build_main_bottom(bottom_frame)

        list_container = tk.Frame(self.root, bg=BG_DARK)
        list_container.pack(side="top", fill="both", expand=True, padx=40, pady=5)

        canvas = tk.Canvas(list_container, bg=BG_DARK, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
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

        if self.edit_mode:
            self._render_main_edit()
        else:
            self._render_main_view()

    def _render_main_view(self):
        for idx, obj in enumerate(self.data):
            row = tk.Frame(self.list_frame, bg=BG_DARK)
            row.pack(fill="x", pady=5)

            name_card = tk.Frame(row, bg=BG_CARD, height=44)
            name_card.pack(side="left", fill="x", expand=True)
            name_card.pack_propagate(False)
            tk.Label(name_card, text=obj["name"], font=("微软雅黑", 11),
                     bg=BG_CARD, fg=TEXT_PRIMARY, anchor="w",
                     padx=15).pack(fill="both", expand=True)

            # 右侧按钮区：截止日期 → 状态 → 进度图 → 详情
            right = tk.Frame(row, bg=BG_DARK)
            right.pack(side="right")

            RoundedButton(right, "详情", command=lambda i=idx: self.show_detail(i),
                          width=65, height=30, bg=ACCENT_BLUE,
                          bg_parent=BG_DARK).pack(side="right")

            RoundedButton(right, "进度图", command=lambda i=idx: self.show_progress_chart(i),
                          width=65, height=30, bg="#6366F1",
                          bg_parent=BG_DARK).pack(side="right", padx=(0, 8))

            StatusBadge(right, obj["status"], bg_parent=BG_DARK).pack(side="right", padx=(0, 8))

            # 截止日期（状态栏最左侧）
            dl_text = obj.get("deadline", "") or "--"
            tk.Label(right, text=dl_text, font=("微软雅黑", 10),
                     bg=BG_DARK, fg=TEXT_SECOND, width=6).pack(side="right", padx=(0, 10))

    def _render_main_edit(self):
        self.name_vars = []
        self.deadline_vars = []
        for idx, obj in enumerate(self.edit_buffer):
            row = tk.Frame(self.list_frame, bg=BG_DARK)
            row.pack(fill="x", pady=5)

            # 名称输入框
            name_var = tk.StringVar(value=obj["name"])
            tk.Entry(row, textvariable=name_var, font=("微软雅黑", 11),
                     relief="flat", bg=BG_INPUT, fg="#202124",
                     insertbackground="#202124").pack(side="left", fill="x", expand=True, ipady=10)
            self.name_vars.append(name_var)

            # 右侧：截止日期输入框 + 状态
            right = tk.Frame(row, bg=BG_DARK)
            right.pack(side="right")

            StatusBadge(right, obj["status"], bg_parent=BG_DARK).pack(side="right", padx=(0, 5))

            dl_var = tk.StringVar(value=obj.get("deadline", ""))
            tk.Entry(right, textvariable=dl_var, width=7, justify="center",
                     font=("微软雅黑", 10), relief="flat", bg=BG_INPUT,
                     fg="#202124").pack(side="right", padx=(0, 8), ipady=5)
            self.deadline_vars.append(dl_var)

    def _build_main_bottom(self, parent):
        if self.edit_mode:
            add_row = tk.Frame(parent, bg=BG_DARK)
            add_row.pack(fill="x", padx=40, pady=(10, 10))

            self.new_obj_entry = tk.Entry(add_row, font=("微软雅黑", 11),
                                          relief="flat", bg=BG_INPUT, fg="#202124",
                                          insertbackground="#202124")
            self.new_obj_entry.pack(side="left", fill="x", expand=True, ipady=10)
            self.new_obj_entry.bind("<Return>", lambda e: self.add_obj())

            RoundedButton(add_row, "添加目标", command=self.add_obj,
                          width=90, height=34, bg=ACCENT_GREEN,
                          bg_parent=BG_DARK).pack(side="left", padx=(12, 0))

            btn_row = tk.Frame(parent, bg=BG_DARK)
            btn_row.pack(fill="x", padx=40, pady=5)

            RoundedButton(btn_row, "全部删除", command=self.delete_all_obj,
                          width=100, height=34, bg=ACCENT_RED,
                          bg_parent=BG_DARK).pack(side="left")

            RoundedButton(btn_row, "取消", command=self.cancel_edit,
                          width=80, height=34, bg="#4B5563",
                          bg_parent=BG_DARK).pack(side="right")
            RoundedButton(btn_row, "确认保存", command=self.save_obj_edit,
                          width=100, height=34, bg=ACCENT_YELLOW, fg="#1F2937",
                          hover_color="#FCD34D", bg_parent=BG_DARK).pack(side="right", padx=(0, 12))
        else:
            RoundedButton(parent, "修改", command=self.enter_main_edit,
                          width=110, height=40, bg=ACCENT_BLUE,
                          font=("微软雅黑", 12, "bold"),
                          bg_parent=BG_DARK).pack(side="right", padx=40, pady=20)

    def enter_main_edit(self):
        self.edit_buffer = copy.deepcopy(self.data)
        self.edit_mode = True
        self.build_main()

    def add_obj(self):
        name = self.new_obj_entry.get().strip()
        if not name:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        self.edit_buffer.append({
            "name": name, "status": "待办",
            "skip_reason": "", "key_results": [],
            "deadline": "",
            "create_time": today,
            # 起点：创建当天剩余进度 100%，作为进度线的第一个点
            "progress_log": [{"date": today, "value": 100.0}]
        })
        self.new_obj_entry.delete(0, tk.END)
        self._refresh_main_list()

    def delete_all_obj(self):
        if messagebox.askyesno("确认", "确定删除全部目标吗？"):
            self.edit_buffer = []
            self._refresh_main_list()

    def _refresh_main_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._render_main_edit()

    def save_obj_edit(self):
        for idx, name_var in enumerate(self.name_vars):
            if idx < len(self.edit_buffer):
                n = name_var.get().strip()
                if n:
                    self.edit_buffer[idx]["name"] = n
        # 保存截止日期
        for idx, dl_var in enumerate(self.deadline_vars):
            if idx < len(self.edit_buffer):
                self.edit_buffer[idx]["deadline"] = dl_var.get().strip()
        for obj in self.edit_buffer:
            self.auto_status(obj)
        self.data = self.edit_buffer
        self.edit_buffer = None
        self.edit_mode = False
        self.save_data()
        self.build_main()

    def cancel_edit(self):
        if messagebox.askyesno("确认", "取消后修改将丢失，确定吗？"):
            self.edit_buffer = None
            self.edit_mode = False
            if self.current_view == "main":
                self.build_main()
            else:
                self.build_detail(self.current_obj_index)

    # ================================================================
    #  详情页（保留原结构，进度保存时触发自动记录）
    # ================================================================
    def show_detail(self, index):
        self.current_obj_index = index
        self.build_detail(index)

    def build_detail(self, index):
        self.clear()
        self.current_view = "detail"
        obj = self.data[index]

        top = tk.Frame(self.root, bg=BG_DARK)
        top.pack(fill="x", padx=25, pady=(20, 10))

        RoundedButton(top, "← 返回", command=self.build_main,
                      width=80, height=32, bg="#4B5563",
                      bg_parent=BG_DARK).pack(side="left")

        tk.Label(top, text=obj["name"], font=("微软雅黑", 17, "bold"),
                 bg=BG_DARK, fg=TEXT_PRIMARY).pack(side="left", padx=15)

        status_frame = tk.Frame(self.root, bg=BG_DARK)
        status_frame.pack(fill="x", padx=40, pady=(5, 10))

        tk.Label(status_frame, text="状态：", font=("微软雅黑", 10),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="left")
        StatusBadge(status_frame, obj["status"], bg_parent=BG_DARK).pack(side="left", padx=5)

        if obj["status"] == "正在处理":
            RoundedButton(status_frame, "标记完成", command=self.mark_complete,
                          width=90, height=28, bg=ACCENT_GREEN,
                          bg_parent=BG_DARK).pack(side="right", padx=5)
            RoundedButton(status_frame, "标记跳过", command=self.mark_skip,
                          width=90, height=28, bg=ACCENT_RED,
                          bg_parent=BG_DARK).pack(side="right", padx=5)
        elif obj["status"] in ("完成", "跳过"):
            RoundedButton(status_frame, "恢复处理", command=self.restore_processing,
                          width=90, height=28, bg=ACCENT_BLUE,
                          bg_parent=BG_DARK).pack(side="right", padx=5)

        if obj["status"] == "跳过" and obj.get("skip_reason"):
            reason_frame = tk.Frame(self.root, bg="#3B1E1E")
            reason_frame.pack(fill="x", padx=40, pady=(0, 10))
            tk.Label(reason_frame, text=f"跳过理由：{obj['skip_reason']}",
                     font=("微软雅黑", 10), bg="#3B1E1E", fg=ACCENT_RED,
                     anchor="w", padx=12, pady=8).pack(fill="x")

        tk.Frame(self.root, height=1, bg=BORDER_COLOR).pack(fill="x", padx=40, pady=(5, 5))

        header = tk.Frame(self.root, bg=BG_DARK)
        header.pack(fill="x", padx=40)
        tk.Label(header, text="关键指标", font=("微软雅黑", 11, "bold"),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="left")
        tk.Label(header, text="完成度", font=("微软雅黑", 11, "bold"),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="right", padx=(0, 10))

        bottom_h = 180 if self.edit_mode else 100
        bottom_frame = tk.Frame(self.root, bg=BG_DARK, height=bottom_h)
        bottom_frame.pack(side="bottom", fill="x")
        bottom_frame.pack_propagate(False)
        self._build_detail_bottom(bottom_frame)

        list_container = tk.Frame(self.root, bg=BG_DARK)
        list_container.pack(side="top", fill="both", expand=True, padx=40, pady=5)

        canvas = tk.Canvas(list_container, bg=BG_DARK, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
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

        if self.edit_mode:
            self._render_detail_edit()
        else:
            self._render_detail_view()

    def _render_detail_view(self):
        obj = self.data[self.current_obj_index]
        for idx, kr in enumerate(obj["key_results"]):
            card = tk.Frame(self.list_frame, bg=BG_CARD)
            card.pack(fill="x", pady=6)

            top_row = tk.Frame(card, bg=BG_CARD)
            top_row.pack(fill="x", padx=14, pady=(10, 6))

            tk.Label(top_row, text=kr["name"], font=("微软雅黑", 10),
                     bg=BG_CARD, fg=TEXT_PRIMARY, anchor="w").pack(side="left")

            pct_label = tk.Label(top_row, text=f"{kr['progress']}%",
                                 font=("微软雅黑", 10, "bold"),
                                 bg=BG_CARD, fg=ACCENT_GREEN)
            pct_label.pack(side="right", padx=(0, 8))

            save_btn = RoundedButton(top_row, "保存",
                                     width=55, height=26,
                                     bg=ACCENT_GREEN,
                                     font=("微软雅黑", 9),
                                     bg_parent=BG_CARD)
            save_btn.pack(side="right")

            slider_row = tk.Frame(card, bg=BG_CARD)
            slider_row.pack(fill="x", padx=14, pady=(0, 10))

            slider = ProgressSlider(
                slider_row, height=12, value=kr["progress"],
                bg_parent=BG_CARD,
                on_preview_change=lambda val, lab=pct_label:
                lab.config(text=f"{val}%")
            )
            slider.pack(fill="x")

            save_btn.command = lambda i=idx, s=slider, lab=pct_label: \
                self.save_single(i, s, lab)

    def save_single(self, idx, slider, pct_label):
        if not slider.is_dirty():
            return
        new_val = slider.save()
        self.data[self.current_obj_index]["key_results"][idx]["progress"] = new_val
        # 保存进度后，检查是否需要记录进度点
        self.maybe_log_progress(self.current_obj_index)
        self.save_data()
        pct_label.config(text=f"{new_val}%")

    def _render_detail_edit(self):
        self.kr_name_vars = []
        self.kr_prog_vars = []
        obj = self.edit_buffer

        for idx, kr in enumerate(obj["key_results"]):
            row = tk.Frame(self.list_frame, bg=BG_DARK)
            row.pack(fill="x", pady=5)

            name_var = tk.StringVar(value=kr["name"])
            tk.Entry(row, textvariable=name_var, font=("微软雅黑", 10),
                     relief="flat", bg=BG_INPUT, fg="#202124",
                     insertbackground="#202124").pack(side="left", fill="x", expand=True, ipady=8)
            self.kr_name_vars.append(name_var)

            right = tk.Frame(row, bg=BG_DARK)
            right.pack(side="right")

            prog_var = tk.StringVar(value=str(kr["progress"]))
            tk.Entry(right, textvariable=prog_var, width=5, justify="center",
                     font=("微软雅黑", 10), relief="flat", bg=BG_INPUT,
                     fg="#202124").pack(side="left", ipady=5)
            tk.Label(right, text="%", bg=BG_DARK, fg=TEXT_SECOND).pack(side="left", padx=2)
            self.kr_prog_vars.append(prog_var)

    def _build_detail_bottom(self, parent):
        if self.edit_mode:
            add_row = tk.Frame(parent, bg=BG_DARK)
            add_row.pack(fill="x", padx=40, pady=(10, 10))

            self.new_kr_entry = tk.Entry(add_row, font=("微软雅黑", 10),
                                         relief="flat", bg=BG_INPUT, fg="#202124",
                                         insertbackground="#202124")
            self.new_kr_entry.pack(side="left", fill="x", expand=True, ipady=9)
            self.new_kr_entry.bind("<Return>", lambda e: self.add_kr())

            RoundedButton(add_row, "添加指标", command=self.add_kr,
                          width=90, height=32, bg=ACCENT_GREEN,
                          bg_parent=BG_DARK).pack(side="left", padx=(12, 0))

            btn_row = tk.Frame(parent, bg=BG_DARK)
            btn_row.pack(fill="x", padx=40, pady=5)

            RoundedButton(btn_row, "全部删除", command=self.delete_all_krs,
                          width=100, height=34, bg=ACCENT_RED,
                          bg_parent=BG_DARK).pack(side="left")

            RoundedButton(btn_row, "取消", command=self.cancel_edit,
                          width=80, height=34, bg="#4B5563",
                          bg_parent=BG_DARK).pack(side="right")
            RoundedButton(btn_row, "确认保存", command=self.save_kr_edit,
                          width=100, height=34, bg=ACCENT_YELLOW, fg="#1F2937",
                          hover_color="#FCD34D", bg_parent=BG_DARK).pack(side="right", padx=(0, 12))
        else:
            RoundedButton(parent, "修改", command=self.enter_detail_edit,
                          width=110, height=40, bg=ACCENT_BLUE,
                          font=("微软雅黑", 12, "bold"),
                          bg_parent=BG_DARK).pack(side="right", padx=40, pady=20)

    def mark_complete(self):
        obj = self.data[self.current_obj_index]
        obj["status"] = "完成"
        self.maybe_log_progress(self.current_obj_index)
        self.save_data()
        self.build_detail(self.current_obj_index)

    def mark_skip(self):
        reason = simpledialog.askstring("跳过理由", "请输入跳过理由：", parent=self.root)
        if reason is None or not reason.strip():
            return
        obj = self.data[self.current_obj_index]
        obj["status"] = "跳过"
        obj["skip_reason"] = reason.strip()
        self.save_data()
        self.build_detail(self.current_obj_index)

    def restore_processing(self):
        obj = self.data[self.current_obj_index]
        obj["status"] = "正在处理" if obj["key_results"] else "待办"
        obj["skip_reason"] = ""
        self.save_data()
        self.build_detail(self.current_obj_index)

    def enter_detail_edit(self):
        self.edit_buffer = copy.deepcopy(self.data[self.current_obj_index])
        self.edit_mode = True
        self.build_detail(self.current_obj_index)

    def add_kr(self):
        name = self.new_kr_entry.get().strip()
        if not name:
            return
        self.edit_buffer["key_results"].append({"name": name, "progress": 0})
        self.new_kr_entry.delete(0, tk.END)
        self._refresh_detail_list()

    def delete_all_krs(self):
        if messagebox.askyesno("确认", "确定删除全部关键指标吗？"):
            self.edit_buffer["key_results"] = []
            self._refresh_detail_list()

    def _refresh_detail_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._render_detail_edit()

    def save_kr_edit(self):
        for idx, name_var in enumerate(self.kr_name_vars):
            if idx < len(self.edit_buffer["key_results"]):
                n = name_var.get().strip()
                if n:
                    self.edit_buffer["key_results"][idx]["name"] = n

        for idx, prog_var in enumerate(self.kr_prog_vars):
            if idx < len(self.edit_buffer["key_results"]):
                try:
                    num = int(prog_var.get().strip())
                    num = max(0, min(100, num))
                    self.edit_buffer["key_results"][idx]["progress"] = num
                except ValueError:
                    pass

        self.auto_status(self.edit_buffer)
        self.data[self.current_obj_index] = self.edit_buffer
        # 保存编辑后记录进度点
        self.maybe_log_progress(self.current_obj_index)
        self.edit_buffer = None
        self.edit_mode = False
        self.save_data()
        self.build_detail(self.current_obj_index)


if __name__ == "__main__":
    root = tk.Tk()
    app = OKRApp(root)
    root.mainloop()