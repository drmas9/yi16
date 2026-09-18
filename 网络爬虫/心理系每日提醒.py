# -*- coding: utf-8 -*-
"""
心理系转专业情报 · 每日必读确认提醒（P2）
==============================================================
运行流程（不带参数时）：
    1. 调用「心理系知识库」同步官网最新通知（只抓第 1 页，约十几秒）；
    2. 弹出置顶、模态的确认窗口，列出所有未读通知（转专业类高亮置顶）；
       —— 右上角关闭按钮（X）无效，必须点击「我已阅读，确认」按钮；
    3. 点击确认后，全部通知标记为已读，窗口才关闭，并写入日志。

每天 18:00 自动运行的安装方式（二选一）：
    · 命令行执行：  python -B 心理系每日提醒.py --install
      （会向 Windows 任务计划程序注册一个每日 18:00 的任务）
    · 不想自动运行：  python -B 心理系每日提醒.py --uninstall 卸载任务

其他参数：
    --test    不联网、不管历史状态，直接弹出测试窗口
    --no-sync 跳过联网同步，只用库里现有数据弹窗（网络出问题时排查用）

设计说明：
    · 弹窗使用 pythonw.exe 运行（无黑色控制台窗口）；
    · 首次运行只建立阅读基线（把现有通知全部标已读），不弹窗，
      从第二天起正常弹窗提醒；
    · 同步失败（断网等）仍然弹窗，明确提示"同步失败"，确认后退出，
      绝不在后台静默丢失。
"""

import argparse
import ctypes
import json
import os
import sys
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

# tkinter 是 Python 官方自带的 GUI 库，无需额外安装
import tkinter as tk
from tkinter import font as tkfont

# 确保无论从哪个工作目录启动（任务计划程序默认在 system32），
# 都能 import 同目录下的中文模块、把数据写回本脚本所在目录
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

import 心理系知识库 as kb  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
STATE_FILE = BASE_DIR / "psych_reminder_state.json"   # 首次基线等状态
LOG_FILE = BASE_DIR / "心理系提醒日志.txt"             # 每次运行的文字日志
TASK_NAME = "心理系转专业每日提醒"                     # Windows 任务计划里的任务名

# 深蓝主题配色（与监控窗口保持同一风格）
COLOR_BG = "#0f2742"        # 窗口底色：深蓝
COLOR_CARD = "#1b3a5c"      # 列表卡片底色
COLOR_TEXT = "#eaf1f8"      # 正文文字
COLOR_MUTED = "#9db4cc"     # 次要文字（日期、说明）
COLOR_HOT = "#ff6b6b"       # 转专业高亮红
COLOR_EXP = "#ffb84d"       # 经验帖高亮橙
COLOR_BTN = "#2f80ed"       # 确认按钮蓝
COLOR_BTN_HOVER = "#5aa0ff"

FONT_MAIN = "微软雅黑"


# ---------------------------------------------------------------------------
# 日志与状态
# ---------------------------------------------------------------------------
def write_log(message: str) -> None:
    """向日志文件追加一行（自动带时间戳）。"""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        # 日志写不了也不应该影响主流程（比如只读介质）
        pass


def load_state() -> dict:
    """读取提醒器状态；文件不存在时返回默认值。"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"initialized": False, "last_run": ""}


def save_state(state: dict) -> None:
    """写回提醒器状态。"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# Windows 任务栏图标闪烁（提醒用户"这个窗口需要处理"）
# ---------------------------------------------------------------------------
def flash_taskbar_icon(root: tk.Tk) -> None:
    """让 Windows 任务栏上的本窗口图标闪烁，直到用户回到窗口。

    对非 Windows 系统自动跳过。
    """
    if not sys.platform.startswith("win"):
        return
    try:
        user32 = ctypes.windll.user32
        # FLASHWINFO 结构体：cbSize / hwnd / dwFlags / uCount / dwTimeout
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]

        info = FLASHWINFO()
        info.cbSize = ctypes.sizeof(FLASHWINFO)
        info.hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        info.dwFlags = 0x00000003 | 0x0000000C   # FLASHW_ALL | FLASHW_TIMERNOFG
        info.uCount = 5
        info.dwTimeout = 0
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        # 闪烁只是增强提醒手段，失败无需处理
        pass


# ---------------------------------------------------------------------------
# 必读确认窗口
# ---------------------------------------------------------------------------
class ReminderWindow:
    """置顶的必读确认窗口。

    参数：
        unread     未读文章行（sqlite3.Row 列表，转专业类已排前）
        sync_note  同步结果说明文字（如"新增 2 篇 · 更新 0 篇"）
        sync_ok    本轮联网同步是否成功（失败时顶部显示警告）
    """

    def __init__(self, unread: list, sync_note: str, sync_ok: bool):
        self.unread = unread
        self.confirmed = False

        self.root = tk.Tk()
        self.root.title("心理系转专业情报 · 每日必读确认")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("760x560")
        self.root.minsize(640, 460)

        # 居中显示在屏幕上
        self.root.update_idletasks()
        w, h = 760, 560
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

        self._build_header(sync_note, sync_ok)
        self._build_list()
        self._build_footer()

        # 关键：拦截右上角 X / Alt+F4 关闭——不读完不能关
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_blocked)
        # 置顶并抢走焦点
        self.root.attributes("-topmost", True)
        self.root.after(200, self._grab_and_flash)

    def _grab_and_flash(self) -> None:
        """窗口显示后：模态独占 + 任务栏闪烁 + 持续置顶。"""
        try:
            self.root.grab_set()           # 模态：不处理本窗口就无法操作别的窗口
            self.root.focus_force()
        except tk.TclError:
            pass
        flash_taskbar_icon(self.root)
        # 每 10 秒重新强调一次置顶（防止被其他全屏窗口盖住），确认后取消
        self._topmost_tick()

    def _topmost_tick(self) -> None:
        if self.confirmed:
            return
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(10_000, self._topmost_tick)

    def _build_header(self, sync_note: str, sync_ok: bool) -> None:
        """顶部标题与同步状态。"""
        top = tk.Frame(self.root, bg=COLOR_BG)
        top.pack(fill="x", padx=24, pady=(20, 8))

        tk.Label(top, text=f"{datetime.now():%Y年%m月%d日} 每日转专业情报核对",
                 bg=COLOR_BG, fg=COLOR_TEXT,
                 font=(FONT_MAIN, 18, "bold")).pack(anchor="w")

        if sync_ok:
            note = f"官网同步完成：{sync_note}"
            note_color = COLOR_MUTED
        else:
            note = f"⚠ 官网同步失败（可能断网），以下为库内未读内容。{sync_note}"
            note_color = COLOR_HOT
        tk.Label(top, text=note, bg=COLOR_BG, fg=note_color,
                 font=(FONT_MAIN, 10)).pack(anchor="w", pady=(6, 0))

    def _build_list(self) -> None:
        """中部：可滚动的未读文章列表；无未读时显示"今日无新通知"。"""
        box = tk.Frame(self.root, bg=COLOR_BG)
        box.pack(fill="both", expand=True, padx=24, pady=8)

        if not self.unread:
            tk.Label(box, bg=COLOR_CARD, fg=COLOR_TEXT,
                     text="\n✓ 今日官网无新通知，知识库已是最新\n仍请点击下方按钮完成每日确认\n",
                     font=(FONT_MAIN, 14), justify="center").pack(fill="both", expand=True)
            return

        # 标题计数
        hot_n = sum(1 for r in self.unread if r["category"] == "转专业")
        exp_n = sum(1 for r in self.unread if r["category"] == "经验帖")
        tip = f"共有 {len(self.unread)} 篇未读通知"
        if hot_n:
            tip += f"，其中 {hot_n} 篇是【转专业】相关（红色标注，请重点看）"
        if exp_n:
            tip += f"，{exp_n} 篇是论坛【经验帖】（橙色标注）"
        tk.Label(box, text=tip, bg=COLOR_BG, fg=COLOR_MUTED,
                 font=(FONT_MAIN, 10), anchor="w").pack(fill="x", pady=(0, 6))

        # 画布 + 滚动条实现可滚动区域
        canvas = tk.Canvas(box, bg=COLOR_CARD, highlightthickness=0, bd=0)
        scroll = tk.Scrollbar(box, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        cards = tk.Frame(canvas, bg=COLOR_CARD)
        canvas.create_window((0, 0), window=cards, anchor="nw", tags="cards")

        def on_configure(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # 让卡片宽度跟随画布，标题能正确换行
            canvas.itemconfig("cards", width=canvas.winfo_width() - 8)
        cards.bind("<Configure>", on_configure)
        canvas.bind("<Configure>", on_configure)
        # 鼠标滚轮滚动
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for r in self.unread:
            cat = r["category"]
            is_hot = cat == "转专业"
            is_exp = cat == "经验帖"
            card = tk.Frame(cards, bg=COLOR_CARD)
            card.pack(fill="x", padx=10, pady=5)

            head = tk.Frame(card, bg=COLOR_CARD)
            head.pack(fill="x")
            tag_color = COLOR_HOT if is_hot else (COLOR_EXP if is_exp else "#7fd1ff")
            tk.Label(head, text=f"[{cat}]", bg=COLOR_CARD, fg=tag_color,
                     font=(FONT_MAIN, 10, "bold")).pack(side="left")
            tk.Label(head, text=r["date"] or "日期未知", bg=COLOR_CARD,
                     fg=COLOR_MUTED, font=(FONT_MAIN, 9)).pack(side="left", padx=(8, 0))

            title_color = "#ffd6d6" if is_hot else ("#ffe2b8" if is_exp else COLOR_TEXT)
            link = tk.Label(card, text=r["title"], bg=COLOR_CARD, fg=title_color,
                            font=(FONT_MAIN, 11, "bold" if (is_hot or is_exp) else "normal"),
                            wraplength=660, justify="left", cursor="hand2")
            link.pack(anchor="w", pady=(2, 0))
            # 点击标题在浏览器打开官网原文
            url = r["href"]
            link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))

    def _build_footer(self) -> None:
        """底部：确认按钮 + 状态提示（点 X 时在此提示）。"""
        bottom = tk.Frame(self.root, bg=COLOR_BG)
        bottom.pack(fill="x", padx=24, pady=(4, 18))

        self.hint = tk.Label(bottom, text="", bg=COLOR_BG, fg=COLOR_HOT,
                             font=(FONT_MAIN, 10))
        self.hint.pack(side="left")

        self.btn = tk.Button(bottom, text="我已逐篇阅读，确认", command=self._on_confirm,
                             bg=COLOR_BTN, fg="white", activebackground=COLOR_BTN_HOVER,
                             activeforeground="white", relief="flat", cursor="hand2",
                             font=(FONT_MAIN, 13, "bold"), padx=28, pady=10, bd=0)
        self.btn.pack(side="right")
        # 鼠标悬停变色
        self.btn.bind("<Enter>", lambda _e: self.btn.configure(bg=COLOR_BTN_HOVER))
        self.btn.bind("<Leave>", lambda _e: self.btn.configure(bg=COLOR_BTN))

    def _on_close_blocked(self) -> None:
        """用户点 X / 按 Alt+F4：拒绝关闭，给出提示并闪烁。"""
        self.hint.configure(text="请先阅读列表，再点击「确认」按钮 ↑")
        flash_taskbar_icon(self.root)
        self.root.bell()

    def _on_confirm(self) -> None:
        """唯一合法的关闭路径：标记已读 → 记录 → 关窗。"""
        self.confirmed = True
        try:
            self.root.grab_release()
        except tk.TclError:
            pass
        self.root.destroy()

    def show(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def fetch_unread() -> list:
    """取出未读文章：转专业类置顶，经验帖次之，其余按日期倒序。"""
    con = kb.get_db()
    try:
        rows = con.execute(
            "SELECT * FROM articles WHERE is_read = 0 "
            "ORDER BY CASE category WHEN '转专业' THEN 0 WHEN '经验帖' THEN 1 ELSE 2 END, "
            "date DESC, id DESC"
        ).fetchall()
        return list(rows)
    finally:
        con.close()


def run(test: bool = False, no_sync: bool = False, with_cc98: bool = True,
        with_bili: bool = True) -> int:
    """执行一次完整的"同步 → 弹窗 → 确认"流程，返回进程退出码。"""
    state = load_state()
    sync_ok = True
    sync_note = "本次未联网"

    # --test：只测试弹窗，不动数据库、不写状态
    if test:
        unread = [] if no_sync else fetch_unread()
        write_log(f"测试弹窗启动，当前未读 {len(unread)} 篇")
        win = ReminderWindow(unread, sync_note="（测试模式，未联网）", sync_ok=True)
        win.show()
        write_log("测试弹窗已确认关闭")
        return 0

    # 第一步：联网同步官网
    if no_sync:
        sync_note = "已跳过同步（--no-sync）"
    else:
        try:
            st = kb.sync(pages=1, all_attachments=False, log=lambda _m: None)
            sync_note = (f"官网新增 {st['new']} 篇 · 更新 {st['updated']} 篇"
                         f" · 无变化 {st['unchanged']} 篇")
            write_log(f"同步完成：{sync_note}")
        except Exception as e:
            sync_ok = False
            sync_note = f"官网同步失败：{type(e).__name__}: {e}"
            write_log("官网同步失败：\n" + traceback.format_exc())

        # 第一步半：采集 CC98 经验帖（失败不影响官网结果与弹窗）
        if with_cc98:
            try:
                import CC98经验帖采集 as cc98_collect
                cs = cc98_collect.collect(dry_run=False,
                                          log=lambda m: write_log("CC98: " + m))
                cc_note = f"；论坛经验帖新入库 {cs['new']} · 更新 {cs['updated']}"
                sync_note += cc_note
                write_log("CC98 采集完成：" + cc_note)
            except Exception:
                sync_note += "；论坛经验帖采集失败（见日志）"
                write_log("CC98 采集失败：\n" + traceback.format_exc())

        # 第一步整：采集 B 站经验视频（失败同样不影响弹窗）
        if with_bili:
            try:
                import B站经验采集 as bili_collect
                bs = bili_collect.collect(dry_run=False, with_subtitle=True,
                                          log=lambda m: write_log("B站: " + m))
                bili_note = (f"；B站经验视频新入库 {bs['new']}"
                             f" · 更新 {bs['updated']}")
                sync_note += bili_note
                write_log("B站采集完成：" + bili_note)
            except Exception:
                sync_note += "；B站经验视频采集失败（见日志）"
                write_log("B站采集失败：\n" + traceback.format_exc())

    # 第二步：首次运行只建立阅读基线，避免一次性弹几十篇旧通知
    if not state.get("initialized"):
        con = kb.get_db()
        try:
            n = kb.mark_all_read(con)
        finally:
            con.close()
        state["initialized"] = True
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        write_log(f"首次运行，建立阅读基线（{n} 篇标记为已读），今日不弹窗")
        return 0

    # 第三步：查未读并弹窗（即使没有未读，也每天要求一次确认）
    unread = fetch_unread()
    write_log(f"弹出确认窗口，未读 {len(unread)} 篇（同步{'成功' if sync_ok else '失败'}）")
    win = ReminderWindow(unread, sync_note, sync_ok)
    win.show()

    # 第四步：只有点了确认按钮才会走到这里
    con = kb.get_db()
    try:
        n = kb.mark_all_read(con)
    finally:
        con.close()
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    write_log(f"用户已点击确认，{n} 篇标记为已读")
    return 0


# ---------------------------------------------------------------------------
# Windows 任务计划的注册 / 卸载
# ---------------------------------------------------------------------------
def _pythonw_path() -> Path:
    """找到与当前 Python 配套的 pythonw.exe（无控制台窗口版）。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def install_task() -> int:
    """向 Windows 任务计划程序注册"每日 18:00 运行本脚本"。"""
    pythonw = _pythonw_path()
    script = BASE_DIR / "心理系每日提醒.py"
    # /TR 的整个命令需要再套一层引号，路径含空格时才不会被拆断
    tr = f'"{pythonw}" -B "{script}"'

    import subprocess
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", tr,
           "/SC", "DAILY", "/ST", "18:00", "/F", "/RL", "LIMITED"]
    print("正在注册每日 18:00 的定时任务……")
    print("任务名：", TASK_NAME)
    print("执行命令：", tr)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="gbk",
                       errors="ignore")
    if r.returncode == 0:
        print("✓ 注册成功！以后每天 18:00 会自动弹出必读确认窗口。")
        write_log("已注册每日 18:00 定时任务")
    else:
        print("✗ 注册失败：", (r.stderr or r.stdout).strip())
        print("可尝试：以管理员身份打开 PowerShell 后重新执行 --install")
    return r.returncode


def uninstall_task() -> int:
    """移除定时任务。"""
    import subprocess
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, text=True, encoding="gbk",
                       errors="ignore")
    if r.returncode == 0:
        print("✓ 已移除每日提醒任务。")
        write_log("已移除定时任务")
    else:
        print("✗ 移除失败（任务可能本来就不存在）：",
              (r.stderr or r.stdout).strip())
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="心理系转专业情报每日必读提醒")
    ap.add_argument("--install", action="store_true", help="注册每日18:00定时任务")
    ap.add_argument("--uninstall", action="store_true", help="移除定时任务")
    ap.add_argument("--test", action="store_true", help="直接弹出测试窗口")
    ap.add_argument("--no-sync", action="store_true", help="跳过所有联网同步")
    ap.add_argument("--no-cc98", action="store_true", help="同步时跳过 CC98 经验帖采集")
    ap.add_argument("--no-bili", action="store_true", help="同步时跳过 B 站经验视频采集")
    args = ap.parse_args()

    try:
        if args.install:
            return install_task()
        if args.uninstall:
            return uninstall_task()
        return run(test=args.test, no_sync=args.no_sync,
                   with_cc98=not args.no_cc98, with_bili=not args.no_bili)
    except Exception:
        # 顶层兜底：任何意外都写进日志，定时任务失败也能查原因
        write_log("运行异常：\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
