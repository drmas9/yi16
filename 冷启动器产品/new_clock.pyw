import tkinter as tk
from tkinter import font, simpledialog
import sys
import os
import ctypes
from ctypes import wintypes
import threading

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

        self.window_w = 400
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

        self.cycle_count = self._load_cycle_count()

        grid_w = GRID_COLS * GRID_CELL_SIZE + (GRID_COLS - 1) * GRID_GAP
        grid_h = GRID_ROWS * GRID_CELL_SIZE + (GRID_ROWS - 1) * GRID_GAP
        self.grid_canvas = tk.Canvas(
            root,
            width=grid_w,
            height=grid_h,
            bg="#222222",
            highlightthickness=0
        )
        self.grid_canvas.pack(pady=(15, 5))
        self.grid_cells = []
        self._create_grid_cells()
        self._update_grid_colors()
        self.grid_canvas.bind("<Button-3>", self.reset_cycle_count)

        self.tray_icon = None
        self._hidden_to_tray = False
        # 浮窗对象
        self.float_window = None
        # 竖向进度条独立窗口
        self.progress_window = None

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

        self.next_task_label = tk.Label(
            root,
            text="",
            font=("Helvetica", 11),
            fg="#ffcc66",
            bg="#222222",
            wraplength=360,
            justify="center"
        )
        self.next_task_label.pack(pady=5)
        self.load_next_task()

        self.button = tk.Button(
            root,
            text="开始",
            command=self.start_countdown,
            font=("Helvetica", 16, "bold"),
            bg="#e53935",
            fg="white",
            activebackground="#c62828",
            activeforeground="white",
            width=12,
            height=2,
            relief="raised",
            bd=3,
            cursor="hand2"
        )
        self.button.pack(pady=10)

        self.taskbar = None
        self.root.after(100, self.init_taskbar)

        if HAS_TRAY:
            self.root.after(200, self.init_tray)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self.on_minimize)

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

        tk.Label(
            self.float_window,
            text="▸ 下一个任务",
            font=("Helvetica", 9),
            fg="#ffcc66",
            bg="#1a1a2e"
        ).pack(anchor="w", padx=10, pady=(6, 0))

        task_text = ""
        try:
            if os.path.exists(NEXT_TASK_FILE):
                with open(NEXT_TASK_FILE, "r", encoding="utf-8") as f:
                    task_text = f.read().strip()
        except:
            pass

        self.float_task_label = tk.Label(
            self.float_window,
            text=task_text if task_text else "（暂无任务）",
            font=("Helvetica", 11),
            fg="#ffffff",
            bg="#1a1a2e",
            wraplength=fw - 20,
            justify="left",
            anchor="w"
        )
        self.float_task_label.pack(anchor="w", padx=10, pady=(2, 6), fill="x")

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
        """销毁竖向进度条窗口"""
        if self.progress_window:
            self.progress_window.destroy()
            self.progress_window = None

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
        self.phase_label.config(text="全部完成！", fg="#66ff66")
        self.button.config(text="重新开始", bg="#e53935", state="normal")
        self.root.configure(bg="#222222")
        self.time_label.config(bg="#222222", fg="#ff4444")
        self.phase_label.config(bg="#222222")
        self.next_task_label.config(bg="#222222")
        self.time_label.config(text="02:00")
        self.grid_canvas.config(bg="#222222")

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
        self.phase = 1
        self.total = 2 * 60        # 2分钟倒计时
        self.remaining = self.total
        self.running = True
        self.time_label.config(fg="#ff4444")
        self.phase_label.config(text="第一阶段：2分钟倒计时", fg="#ff6666")
        self.button.config(text="倒计时中...", state="disabled", bg="#999999")

        if self.taskbar:
            self.taskbar.set_state(2)
            self.taskbar.set_progress(0, self.total)

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
            self.next_task_label.config(bg="#112244")
            self.grid_canvas.config(bg="#112244")

            self.button.config(text="进行中", bg="#1565c0", state="disabled")

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


if __name__ == "__main__":
    root = tk.Tk()
    app = CountdownClock(root)
    root.mainloop()