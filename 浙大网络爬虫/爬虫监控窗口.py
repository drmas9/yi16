# -*- coding: utf-8 -*-
"""
浙江大学新闻 + CC98 论坛监控窗口
==============================

这个程序是在两个爬虫模块之上构建的一个图形界面（GUI）监控程序：
1. 「网络爬虫实验.py」—— 负责抓取浙江大学官网新闻（提供 fetch / parse_home / build_news 等函数）
2. 「cc98爬虫.py」     —— 负责抓取 CC98 论坛（提供 Cc98Client 类、build_items / sort_items 等函数）

主要功能：
- 深蓝色主题的卡片式界面（样式参考「OKR实验品.py」）
- 顶部可切换视图：浙大新闻 / CC98论坛
- 可按分类筛选浏览消息
- 后台线程定时抓取所有来源，发现新消息时实时提醒（弹窗 + 标题栏标记 + 列表高亮）
- CC98 视图中「讲座」「实习求职」类优先展示

如何新增一个信息源（重点）：
    本程序用一张「信息源注册表」SOURCES 来管理所有来源。界面代码完全不关心
    具体有哪些来源，它只是遍历 SOURCES 来生成按钮、抓取数据、渲染卡片。
    所以新增一个来源只需要做两件事，不用改动任何界面代码：

    1. 写一个抓取函数，接收一个 state 字典，返回「统一格式的记录列表」：
           def fetch_xxx(state):
               return [ {title, date, href, summary, source, category, meta}, ... ]
       state 是这个来源自己的运行时状态（比如登录用的客户端），会在多次抓取
       之间一直保留，用来缓存那些创建代价高的对象。

    2. 调用 register_source(...) 把它的元信息和抓取函数登记进去。

    登记完成后，界面上会自动多出这个来源的切换按钮、分类筛选条和消息列表。

运行方式：
    python 爬虫监控窗口.py

依赖：仅使用 Python 标准库（tkinter），无需安装第三方包。
"""

import html          # 用于把 &nbsp; 这类 HTML 转义字符还原成普通文字
import json          # 用于读写 JSON 文件（保存"已经看过的消息链接"）
import os            # 用于拼接文件路径
import re            # 用于正则表达式（去掉 HTML 标签）
import sys           # 用于把当前目录加入模块搜索路径，方便导入同目录的爬虫模块
import threading     # 用于启动后台线程（抓取数据时不卡住界面）
import time          # 用于休眠等待（轮询间隔）
import tkinter as tk # tkinter 是 Python 自带的图形界面库，tk 是它的常用别名
from datetime import datetime  # 用于记录时间
from tkinter import messagebox, ttk  # messagebox：弹窗；ttk：带样式的控件（滚动条）

# 把本文件所在的目录加入 Python 模块搜索路径
# 这样下面才能用 import 的方式导入同目录下的两个爬虫模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import 网络爬虫实验 as zju_crawler   # noqa: E402  # 导入浙大新闻爬虫，起个别名 zju_crawler
import cc98爬虫 as cc98_crawler     # noqa: E402  # 导入 CC98 论坛爬虫，起个别名 cc98_crawler

# ===========================================================================
# 配色方案（与 OKR实验品.py 保持一致的深蓝主题）
# 这些是十六进制颜色值，供界面控件使用，方便统一改主题
# ===========================================================================
BG_DARK = "#0F2B5B"        # 窗口最外层背景：深蓝
BG_CARD = "#1A3A6B"        # 卡片背景：稍亮一点的蓝色
BG_INPUT = "#FFFFFF"       # 输入框背景：白色
TEXT_PRIMARY = "#FFFFFF"   # 主要文字颜色：白色
TEXT_SECOND = "#8BA4CC"    # 次要文字颜色：浅蓝灰
ACCENT_BLUE = "#4A90FF"    # 强调色：蓝色（按钮、链接）
ACCENT_GREEN = "#34D399"   # 强调色：绿色（"立即刷新"按钮）
ACCENT_RED = "#F87171"     # 强调色：红色（"新消息"标记）
ACCENT_YELLOW = "#FBBF24"  # 强调色：黄色
ACCENT_ORANGE = "#FB923C"  # 强调色：橙色
BORDER_COLOR = "#2A4A7A"   # 分隔线颜色

# 浙大新闻分类对应的颜色（每个分类一个颜色，界面上用小胶囊标签显示）
CATEGORY_COLORS = {
    "校务管理": ACCENT_BLUE,
    "科研学术": ACCENT_GREEN,
    "人物报道": ACCENT_YELLOW,
    "讲座公告": ACCENT_ORANGE,
    "展览文体": "#EC4899",
    "招生就业": "#A78BFA",
    "国际合作": "#22D3EE",
    "社会服务": ACCENT_GREEN,
    "媒体聚焦": TEXT_SECOND,
    "其他": "#6B7280",
}

# CC98 论坛分类对应的颜色
CC98_COLORS = {
    "讲座": ACCENT_ORANGE,
    "实习求职": "#A78BFA",
    "留学": "#22D3EE",
    "考研": "#EC4899",
    "活动": ACCENT_GREEN,
    "其他": "#6B7280",
}

# 竞赛系统分类对应的颜色
KYJS_COLORS = {
    "竞赛通知": ACCENT_GREEN,
    "竞赛结果": ACCENT_ORANGE,
    "科研训练": "#A78BFA",
}

# 后台轮询间隔（秒）
DEFAULT_INTERVAL = 30 * 60  # 默认 30 分钟自动抓取一次

# 数据文件路径：程序会把"已经看过的消息链接"保存到本地 JSON 文件，
# 下次抓取时对比这些链接，就能知道哪些是新消息
DATA_DIR = os.path.dirname(os.path.abspath(__file__))


# ===========================================================================
# 信息源注册表（这是整个程序的扩展点）
# ===========================================================================
# 统一记录格式说明 —— 每个抓取函数都必须返回「字典的列表」，每个字典包含这些键：
#   title    标题（字符串）
#   date     日期，格式固定为 "YYYY-MM-DD"（字符串，排序和显示都依赖它）
#   href     消息的唯一链接（字符串，程序靠它判断"是否是新消息"，必须唯一）
#   summary  摘要（字符串，可为空）
#   source   来源名，例如 CC98 的板块名（字符串，可为空）
#   category 分类名，必须落在该来源的 categories 列表里，否则筛选时看不到
#   meta     卡片上紧跟"来源"显示的一小段附加信息，例如"回复 12 / 浏览 340"
#            （字符串，可为空；这是为了让卡片渲染逻辑对所有来源统一）
class SourceConfig:
    """描述一个信息源：界面怎么显示它、用哪个函数抓它、它有哪些分类。

    只要把一个新的 SourceConfig 登记到 SOURCES 里，界面就会自动多出一个视图，
    不需要在下面的界面代码里加任何 if/else 分支。
    """

    def __init__(self, key, label, title, fetcher, categories, colors, seen_file):
        # key：内部代号，用来在 self.data 里索引这个来源的数据，如 "zju"
        self.key = key
        # label：顶部切换按钮上显示的文字，如 "浙大新闻"
        self.label = label
        # title：窗口标题，如 "浙江大学新闻监控"
        self.title = title
        # fetcher：抓取函数，签名必须是 fetcher(state) -> list[dict]
        self.fetcher = fetcher
        # categories：分类筛选条上的分类名列表，第一项通常是"全部"
        self.categories = categories
        # colors：分类名 -> 十六进制颜色，用于卡片上的分类胶囊；找不到时用灰色
        self.colors = colors
        # seen_file：保存"已经看过的链接"的本地 JSON 文件路径
        self.seen_file = seen_file
        # state：这个来源自己的运行时状态字典，会被原样传给 fetcher，
        #        并且在多次抓取之间一直保留（用来缓存登录客户端这类昂贵对象）
        self.state = {}


# 全局注册表：key -> SourceConfig。界面的所有循环都遍历它。
SOURCES = {}


def register_source(cfg):
    """把一个信息源登记到注册表里。登记后界面就会自动出现对应的视图。"""
    SOURCES[cfg.key] = cfg


# ---------------------------------------------------------------------------
# 各来源的抓取函数（把原爬虫模块的输出转成上面说的统一记录格式）
# ---------------------------------------------------------------------------
def fetch_zju_news(state):
    """抓取浙大官网新闻，返回统一记录列表（已按日期从新到旧排序）。

    state 参数只是为了和其他来源的抓取函数保持相同签名，这里用不到。
    """
    # 第1步：请求浙大官网首页，拿到网页 HTML 源代码
    html = zju_crawler.fetch(zju_crawler.HOME_URL)
    # 第2步：从 HTML 中解析出新闻列表的"原始数据"
    raw = zju_crawler.parse_home(html)
    # 第3步：把原始数据整理成统一的新闻字典格式（含分类）
    news = zju_crawler.build_news(raw)
    # 第4步：按日期从新到旧排序
    news.sort(key=lambda n: n["date"], reverse=True)
    # 第5步：补上统一的 meta 字段（浙大新闻没有"回复/浏览"这类附加信息）
    for n in news:
        n["meta"] = ""
    return news


def fetch_cc98_news(state):
    """抓取 CC98 论坛，返回统一记录列表。

    state 里缓存着登录客户端：CC98 接口需要登录，而登录要发一次网络请求，
    把客户端存进 state 就可以复用登录状态，避免每次轮询都重新登录。
    """
    client = state.get("client")
    if client is None:
        client = cc98_crawler.Cc98Client(
            cc98_crawler.CC98_USERNAME, cc98_crawler.CC98_PASSWORD)
        client.login()          # 执行登录
        state["client"] = client  # 存进 state，下次抓取直接复用
    # 抓取各板块话题并整理成统一格式的消息列表
    items = cc98_crawler.build_items(client)
    # 按优先级排序（讲座 > 实习求职 > 其他，同级按时间倒序）
    items = cc98_crawler.sort_items(items)
    # 把 CC98 特有的"回复数/浏览数"拼成一段文字塞进统一字段 meta
    for n in items:
        n["meta"] = f"回复 {n.get('reply', 0)} / 浏览 {n.get('hit', 0)}"
    return items


# ---------------------------------------------------------------------------
# 浙大本科生科研训练与学科竞赛管理系统（kyjs.zju.edu.cn）
# ---------------------------------------------------------------------------
# 这个站是 Vue 单页应用：网页 HTML 里只有一个空的 <div id="app">，真正的数据
# 由页面里的 JavaScript 再去请求后端接口拿回来。所以不能像浙大官网那样"抓网页
# 再解析 HTML"，而要直接调用它的后端 JSON 接口。
#
# 接口信息（2026-09 确认）：
#   GET /prod-api/home/news/page?pageNum=1&pageSize=50&topicId=10000
#   - 公开只读，不需要登录、不需要 token；
#   - 返回 JSON：data.records 是消息数组，data.total 是总数。
#
# 栏目（topicId）：10000 = 通知公告（竞赛举办/报名通知、获奖名单公布）
#                 10001 = 竞赛成果（获奖新闻）    10005 = 新闻动态
# 本爬虫只抓 10000，因为它才是"要你去报名/参加"的那类信息。
KYJS_API = "http://kyjs.zju.edu.cn/prod-api/home/news/page"
KYJS_DETAIL = "http://kyjs.zju.edu.cn/preview/detail"
KYJS_TOPIC_ID = 10000
KYJS_TOPIC_NAME = "通知公告"     # 详情页链接里要带这个栏目名参数
KYJS_PAGE_SIZE = 50              # 每次抓最新的 50 条，足够覆盖轮询间隔内的新增

# 竞赛通知的内部分类关键词。
# 判断顺序很重要：先看是不是"科研训练"（国创/新苗这类项目申报），
# 再看是不是"竞赛结果"（获奖名单），都不是就算作普通的"竞赛通知"。
KYJS_KEYWORDS = {
    "科研训练": ["创新训练计划", "创新创业训练", "大学生创新创业", "国创",
                 "新苗", "科研训练", "srtp"],
    "竞赛结果": ["获奖", "结果", "公示", "名单", "公布", "揭晓", "表彰"],
}


def _strip_html(text):
    """把一段 HTML 正文变成纯文本，用来当摘要显示。

    竞赛系统返回的 content 字段是一整段 HTML（带 <p>、<span>、<table> 等标签），
    这里把标签去掉、把 &nbsp; 之类的转义还原、把连续空白压成一个空格。
    """
    text = re.sub(r"<[^>]+>", "", text or "")   # 去掉所有 <...> 标签
    text = html.unescape(text)                  # &nbsp; &amp; 等还原成普通字符
    text = re.sub(r"\s+", " ", text)            # 连续空白（含换行）压成一个空格
    return text.strip()


def _classify_kyjs(title):
    """按标题关键词判断竞赛通知属于哪一类。"""
    t = title.lower()
    for cat in ("科研训练", "竞赛结果"):   # 顺序固定，先匹配更具体的
        if any(k in t for k in KYJS_KEYWORDS[cat]):
            return cat
    return "竞赛通知"


def fetch_kyjs_news(state):
    """抓取竞赛系统「通知公告」栏目，返回统一记录列表（已按日期倒序）。

    state 参数只是为了和其他来源的抓取函数保持相同签名，这里用不到。
    """
    # 拼出接口地址，然后直接拿 JSON。这里复用了浙大新闻爬虫里的 fetch 函数
    # （它只是"带浏览器请求头去 GET 一个网址并返回文本"，与具体网站无关）。
    url = f"{KYJS_API}?pageNum=1&pageSize={KYJS_PAGE_SIZE}&topicId={KYJS_TOPIC_ID}"
    data = json.loads(zju_crawler.fetch(url))
    if data.get("code") != 0:
        # code 不为 0 说明接口返回了错误（例如接口改版），抛出异常让上层提示
        raise RuntimeError(f"竞赛系统接口返回异常：{data.get('msg')}")
    records = (data.get("data") or {}).get("records") or []

    items = []
    for r in records:
        news_id = r.get("newsId")                    # 消息编号，用来拼详情页链接
        title = (r.get("title") or "").strip()
        publish = (r.get("publishTime") or "").strip()   # 形如 "2026-09-15 17:24:05"
        # 缺关键字段的条目直接跳过，避免脏数据导致后面报错
        if not news_id or not title or len(publish) < 10:
            continue
        items.append({
            "title": title,
            # 只取日期部分："2026-09-15 17:24:05" -> "2026-09-15"
            "date": publish[:10],
            "href": f"{KYJS_DETAIL}?newsId={news_id}&typeName={KYJS_TOPIC_NAME}",
            "summary": _strip_html(r.get("content"))[:300],
            "source": "本科生院·竞赛系统",
            "category": _classify_kyjs(title),
            "meta": f"浏览 {r.get('pageViews', 0)}",
        })
    # 按日期从新到旧排序（字符串日期格式统一，直接按字符串比较即可）
    items.sort(key=lambda n: n["date"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# 登记各个信息源
# ---------------------------------------------------------------------------
register_source(SourceConfig(
    key="zju",
    label="浙大新闻",
    title="浙江大学新闻监控",
    fetcher=fetch_zju_news,
    # 浙大新闻的分类："全部" + 爬虫模块定义的所有分类 + "其他"
    categories=["全部"] + list(zju_crawler.CATEGORIES.keys()) + ["其他"],
    colors=CATEGORY_COLORS,
    seen_file=os.path.join(DATA_DIR, "zju_seen.json"),
))

register_source(SourceConfig(
    key="cc98",
    label="CC98论坛",
    title="CC98论坛监控",
    fetcher=fetch_cc98_news,
    # CC98 的分类："全部" + 爬虫模块定义好的分类顺序
    categories=["全部"] + list(cc98_crawler.CATEGORY_ORDER),
    colors=CC98_COLORS,
    seen_file=os.path.join(DATA_DIR, "cc98_seen.json"),
))

register_source(SourceConfig(
    key="kyjs",
    label="竞赛通知",
    title="浙大竞赛通知监控",
    fetcher=fetch_kyjs_news,
    categories=["全部", "竞赛通知", "竞赛结果", "科研训练"],
    colors=KYJS_COLORS,
    seen_file=os.path.join(DATA_DIR, "kyjs_seen.json"),
))

# 程序启动时默认显示哪个来源
DEFAULT_SOURCE = "zju"


# ===========================================================================
# UI 组件（圆角按钮，与 OKR实验品 一致）
# ===========================================================================
class RoundedButton(tk.Canvas):
    """圆角按钮 — 用 Canvas 画布手工绘制，带鼠标悬停(hover)变色效果。

    为什么不用 tk.Button？因为 tk.Button 不支持圆角样式，
    所以这里用一个 Canvas（画布）来画：左右两个半圆 + 中间一个矩形，
    拼在一起就形成了圆角矩形的外观。
    """

    def __init__(self, master, text, command=None, width=80, height=32,
                 bg=ACCENT_BLUE, fg="white", hover_color=None,
                 font=("微软雅黑", 10), bg_parent=BG_DARK, **kwargs):
        # master：按钮放在哪个容器里；text：按钮文字；command：点击按钮时要执行的函数
        super().__init__(master, width=width, height=height,
                         bg=bg_parent, highlightthickness=0, **kwargs)
        self.command = command
        self.text = text
        self.bg = bg                          # 按钮正常状态的颜色
        self.fg = fg                          # 文字颜色
        # 悬停颜色：如果没有指定，就自动把背景色调亮一点
        self.hover_color = hover_color or self._lighten(bg, 25)
        self.font = font
        self.radius = height // 2             # 圆角的半径，取高度的一半
        self.width = width
        self.height = height
        self._draw_button(bg)                 # 立刻画一次按钮
        # 绑定鼠标事件：<Button-1> 左键点击、<Enter> 鼠标进入、<Leave> 鼠标离开
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw_button(self, color):
        """把按钮画出来：两个圆 + 一个矩形 = 圆角矩形，再写上文字。"""
        self.delete("all")                    # 清空画布上旧的内容
        r = self.radius
        w, h = self.width, self.height
        # 左下角、右下角两个圆
        self.create_oval(0, 0, 2 * r, 2 * r, fill=color, outline=color)
        self.create_oval(w - 2 * r, 0, w, 2 * r, fill=color, outline=color)
        # 中间填充的矩形
        self.create_rectangle(r, 0, w - r, h, fill=color, outline=color)
        # 居中写上按钮文字
        self.create_text(w / 2, h / 2, text=self.text, fill=self.fg, font=self.font)

    def _on_enter(self, e):
        """鼠标移进按钮时，用悬停颜色重画一遍（产生变色效果）。"""
        self._draw_button(self.hover_color)

    def _on_leave(self, e):
        """鼠标移出按钮时，恢复成原来的颜色。"""
        self._draw_button(self.bg)

    def _on_click(self, e):
        """鼠标左键点击按钮时，调用之前传入的 command 函数。"""
        if self.command:
            self.command()

    def _lighten(self, hex_color, amount):
        """把十六进制颜色调亮 amount 个单位，返回新的十六进制颜色字符串。"""
        hex_color = hex_color.lstrip("#")          # 去掉开头的 #
        # 把 "#RRGGBB" 拆成 R、G、B 三个 0~255 的整数
        r, g, b = [int(hex_color[i:i + 2], 16) for i in (0, 2, 4)]
        r = min(255, r + amount)                   # 每个分量都加上 amount，但不能超过 255
        g = min(255, g + amount)
        b = min(255, b + amount)
        return f"#{r:02x}{g:02x}{b:02x}"           # 再拼回 "#RRGGBB" 格式


class CategoryChip(tk.Canvas):
    """分类标签胶囊：就是消息卡片上那个有颜色的小标签，用来标识消息属于哪个分类。"""

    def __init__(self, master, text, color="#6B7280", width=None, height=24,
                 bg_parent=BG_DARK, **kwargs):
        self.text_str = text
        # 自动计算宽度：如果没指定宽度，就按文字长度估算（每个字大约 12 像素）
        if width is None:
            width = max(40, len(text) * 12 + 18)
        super().__init__(master, width=width, height=height,
                         bg=bg_parent, highlightthickness=0, **kwargs)
        self.color = color
        self.width = width
        self.height = height
        self.radius = height // 2     # 胶囊两端是半圆，所以圆角半径也是高度的一半
        self._draw()

    def _draw(self):
        """和 RoundedButton 一样，用"两个圆 + 一个矩形"画出胶囊形状，中间写上分类名。"""
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
class NewsMonitorApp:
    """监控程序的主窗口类：负责界面布局、数据抓取、后台轮询和新消息提醒。"""

    def __init__(self, root):
        self.root = root                          # 保存主窗口对象
        self.root.geometry("860x640")             # 设置窗口大小：宽 860 像素，高 640 像素
        self.root.configure(bg=BG_DARK)           # 设置窗口背景为深蓝色

        # 视图与数据：每个来源独立维护一套数据
        self.mode = DEFAULT_SOURCE                # 当前显示的视图，默认是"浙大新闻"
        # self.data 是核心数据结构：按来源代号各存一套数据，例如
        # data["zju"]、data["cc98"]。每套数据包含：
        #   all_news       -> 抓取到的全部消息列表
        #   displayed_news -> 当前界面实际显示的消息列表（经过分类筛选）
        #   new_hrefs      -> 本次运行中标记为"新消息"的链接集合
        #   category       -> 当前选中的分类筛选（"全部"表示不过滤）
        # 注意：这里直接遍历 SOURCES，所以新增来源时这里不需要改动
        self.data = {}          # 来源代号 -> {all_news, displayed_news, new_hrefs, category}
        for key in SOURCES:
            self.data[key] = {
                "all_news": [],
                "displayed_news": [],
                "new_hrefs": set(),
                "category": "全部",
            }
        self.list_frame = None                    # 放置消息卡片的容器（后面 build_main 里创建）

        # 后台轮询相关变量
        self.interval_sec = DEFAULT_INTERVAL      # 轮询间隔（秒），用户可在界面修改
        self.running = True                       # 程序是否在运行（关闭窗口时设为 False）
        self.worker = None                        # 后台线程对象
        self.last_check_time = None               # 最近一次成功抓取的时间（用于状态栏显示）

        # 初始抓取：程序刚启动时先把每个来源都抓一遍
        # initial=True 表示首次抓取（失败时不弹窗，避免启动时打扰用户）
        for key in SOURCES:
            self._do_fetch(key, initial=True, show_error=False)

        # 构建界面
        self.build_main()

        # 启动后台线程：后台线程负责定时抓取数据
        self._start_worker()

        # 绑定窗口关闭事件：点窗口右上角"×"时执行 on_close（让后台线程安全退出）
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ 数据
    def _load_seen(self, view):
        """从本地 JSON 文件读取"已经看过的消息链接"，返回一个集合(set)。

        如果文件不存在或内容损坏，就返回空集合。
        """
        path = SOURCES[view].seen_file
        if os.path.exists(path):
            try:
                return set(json.loads(open(path, encoding="utf-8").read())
                           .get("hrefs", []))
            except Exception:
                return set()
        return set()

    def _save_seen(self, view, hrefs):
        """把"已经看过的消息链接"保存到本地 JSON 文件，下次启动/轮询时用来对比。"""
        path = SOURCES[view].seen_file
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"hrefs": sorted(hrefs),
                       "updated": datetime.now().isoformat(timespec="seconds")},
                      f, ensure_ascii=False, indent=2)

    def _do_fetch(self, view, initial=False, show_error=True):
        """抓取指定来源的数据，抓取成功后存入 self.data[view]["all_news"]。

        参数：
            view        : 信息来源的代号（即 SOURCES 里的 key），如 "zju" / "cc98"
            initial     : 是否首次抓取
            show_error  : 抓取失败时是否弹窗提示
        返回值：抓取成功返回 True，失败返回 False。
        """
        src = SOURCES[view]
        try:
            # 具体怎么抓、怎么解析、怎么分类排序，全都封装在这个来源自己的
            # fetcher 函数里（见文件上方的 fetch_zju_news / fetch_cc98_news）。
            # 这里只负责调用它，并把结果存起来供界面渲染使用。
            # 注意：fetcher 是阻塞式的网络请求，所以它始终在后台线程里被调用。
            self.data[view]["all_news"] = src.fetcher(src.state)
            return True
        except Exception as e:
            # 抓取失败：如果允许弹窗，就在主线程里弹出错误提示
            if show_error:
                self.root.after(0, lambda: messagebox.showerror("抓取失败", str(e)))
            return False

    # ------------------------------------------------------------------ UI
    def build_main(self):
        """构建整个界面。因为布局会随视图/分类变化，所以每次都要先清空再重建。"""
        # 清空窗口里已有的所有控件（切换视图、切换分类时界面整体重建）
        for w in self.root.winfo_children():
            w.destroy()

        # 顶部标题区：视图切换按钮 + 标题 + 状态
        top = tk.Frame(self.root, bg=BG_DARK)          # Frame 是一个容器控件
        top.pack(fill="x", padx=25, pady=(18, 10))     # pack 布局：横向填满，四周留边距

        # 视图切换按钮（浙大新闻 / CC98论坛 ...）。遍历 SOURCES 自动生成，
        # 所以以后新增来源时，这里会自动多出一个按钮，不用改这段代码。
        for key, src in SOURCES.items():
            is_active = (key == self.mode)   # 当前选中的视图按钮用亮蓝色，其他用暗色
            RoundedButton(
                top, src.label, command=lambda k=key: self.switch_view(k),
                width=92, height=30,
                bg=ACCENT_BLUE if is_active else BG_CARD,
                hover_color=ACCENT_BLUE,
                font=("微软雅黑", 10, "bold" if is_active else "normal"),
                bg_parent=BG_DARK,
            ).pack(side="left", padx=(0, 8))   # 从左往右排列

        # 中间的标题文字，例如"浙江大学新闻监控（36 条）"
        tk.Label(top, text=self._view_title_text(), font=("微软雅黑", 15, "bold"),
                 bg=BG_DARK, fg=TEXT_PRIMARY).pack(side="left", padx=(8, 0))

        # 右侧的状态文字：上次检查时间、轮询间隔
        self.status_label = tk.Label(
            top, text=self._status_text(), font=("微软雅黑", 9),
            bg=BG_DARK, fg=TEXT_SECOND)
        self.status_label.pack(side="right")   # 靠右排列

        # 分类筛选条：一排分类按钮（如"全部""校务管理""科研学术"...）
        cat_bar = tk.Frame(self.root, bg=BG_DARK)
        cat_bar.pack(fill="x", padx=25, pady=(0, 8))
        self._build_category_bar(cat_bar)

        # 分隔线：一条细线把标题区和下面的列表区分开
        tk.Frame(self.root, height=1, bg=BORDER_COLOR).pack(fill="x", padx=25, pady=2)

        # 滚动列表：用 Canvas + 滚动条 + 内部 Frame 实现
        # 原理：把很多消息卡片放进 list_frame，list_frame 放在 Canvas 上，
        # Canvas 本身是"窗口大小"，滚动条拖动时移动显示区域，实现滚动效果
        list_container = tk.Frame(self.root, bg=BG_DARK)
        list_container.pack(side="top", fill="both", expand=True, padx=25, pady=4)

        canvas = tk.Canvas(list_container, bg=BG_DARK, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical",
                                  command=canvas.yview)   # 滚动条控制 Canvas 纵向滚动
        self.list_frame = tk.Frame(canvas, bg=BG_DARK)

        # 当 list_frame 内容尺寸变化时（比如列表变长），更新 Canvas 的滚动范围
        self.list_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        # 把 list_frame 作为一个"窗口"放上 Canvas（挂在左上角，垂直方向自动布局）
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw",
                             tags=("list_window",))
        # 当 Canvas 宽度变化时，让 list_frame 跟着变宽（保证卡片铺满）
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig("list_window", width=e.width))
        # 反过来：滚动条被拖动时，通知 Canvas 滚动
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 绑定鼠标滚轮：让滚轮也能滚动列表（e.delta 是滚动量，/120 换算成滚动单位数）
        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

        # 底部操作区：立即刷新、间隔设置、清空新消息标记
        bottom = tk.Frame(self.root, bg=BG_DARK, height=70)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)   # 固定高度 70，不随内容自动伸缩
        self._build_bottom(bottom)

        # 最后把当前视图的消息渲染成一张张卡片
        self._render_list()

    def _view_title_text(self):
        """生成顶部标题文字，例如"浙江大学新闻监控（36 条）"。"""
        d = self.data[self.mode]
        n = len(d["all_news"])   # 当前视图抓到的消息总数
        return f"{SOURCES[self.mode].title}（{n} 条）"

    def _status_text(self):
        """生成状态栏文字：上次检查时间 + 轮询间隔。"""
        t = self.last_check_time
        ts = t.strftime("%H:%M:%S") if t else "—"   # 格式化时间，没检查过就显示"—"
        return f"上次检查：{ts}    间隔：{self.interval_sec // 60} 分钟"

    def _build_category_bar(self, parent):
        """在分类条容器里生成一排分类按钮。点击某个分类后只显示该分类的消息。"""
        d = self.data[self.mode]     # 当前视图的数据（下面要用 d["category"] 判断选中项）
        # 分类列表来自注册表（见上方 SourceConfig 的 categories 字段），
        # 所以每个来源有哪些分类由它自己定义，这里不需要判断是哪个来源。
        cats = SOURCES[self.mode].categories
        for cat in cats:
            is_active = (cat == d["category"])     # 当前选中的分类高亮显示
            bg = ACCENT_BLUE if is_active else BG_CARD
            fg = "white"
            font = ("微软雅黑", 9, "bold") if is_active else ("微软雅黑", 9)
            # 这里直接用 Label 当按钮（带 cursor="hand2" 显示小手光标）
            btn = tk.Label(
                parent, text=cat, font=font, bg=bg, fg=fg,
                padx=10, pady=5, cursor="hand2"
            )
            btn.pack(side="left", padx=(0, 6))
            # 点击分类时调用 select_category
            # 注意：用 c=cat 把分类名"绑定"进 lambda，避免闭包陷阱（否则所有按钮都得到最后一个 cat）
            btn.bind("<Button-1>", lambda e, c=cat: self.select_category(c))

    def _build_bottom(self, parent):
        """构建底部操作区：立即刷新按钮、轮询间隔设置、清空新消息标记按钮。"""
        # "立即刷新"按钮：点击后立刻在后台线程抓取一次数据
        RoundedButton(parent, "立即刷新", command=self.manual_refresh,
                      width=100, height=34, bg=ACCENT_GREEN,
                      bg_parent=BG_DARK).pack(side="left", padx=25, pady=18)

        # 轮询间隔设置：一个文字标签 + 一个 Spinbox（数字输入框，1~180 分钟）
        tk.Label(parent, text="轮询间隔(分)：", font=("微软雅黑", 9),
                 bg=BG_DARK, fg=TEXT_SECOND).pack(side="left", padx=(10, 5))
        self.interval_var = tk.StringVar(value=str(self.interval_sec // 60))  # 输入框的值
        spin = tk.Spinbox(parent, from_=1, to=180, width=5,
                          textvariable=self.interval_var, font=("微软雅黑", 9),
                          bg=BG_INPUT, fg="#202124", relief="flat",
                          command=self._on_interval_change)   # 点上下箭头时触发
        spin.pack(side="left", ipady=4)
        # 手动输入数字也触发：StringVar 值一变就调用 _on_interval_change
        self.interval_var.trace_add("write", lambda *_: self._on_interval_change())

        # 右侧："清空新消息标记"按钮，把所有红色"● 新"标记去掉
        RoundedButton(parent, "清空新消息标记", command=self.clear_new_flags,
                      width=130, height=34, bg="#4B5563",
                      bg_parent=BG_DARK).pack(side="right", padx=25, pady=18)

    # ------------------------------------------------------------------ 列表渲染
    def _render_list(self):
        """把当前视图的消息渲染成界面上的卡片列表。先清空旧卡片再重新生成。"""
        # 删除 list_frame 里所有旧控件（重新渲染）
        for w in self.list_frame.winfo_children():
            w.destroy()

        d = self.data[self.mode]
        # 根据当前分类筛选消息
        if d["category"] == "全部":
            d["displayed_news"] = list(d["all_news"])
        else:
            d["displayed_news"] = [n for n in d["all_news"]
                                   if n["category"] == d["category"]]

        # 筛选后没有消息就显示一行提示文字
        if not d["displayed_news"]:
            tk.Label(self.list_frame, text="（暂无消息）",
                     font=("微软雅黑", 11), bg=BG_DARK, fg=TEXT_SECOND
                     ).pack(pady=40)
            return

        # 逐条渲染：每条消息画成一张卡片
        for n in d["displayed_news"]:
            self._render_card(n)

    def _render_card(self, n):
        """把一条消息 n 渲染成一张卡片（日期/分类/标题/摘要/链接 + 打开按钮）。"""
        d = self.data[self.mode]
        is_new = n["href"] in d["new_hrefs"]     # 判断这条消息是否是新消息
        card_bg = "#243F73" if is_new else BG_CARD   # 新消息的卡片用更亮的蓝色背景
        card = tk.Frame(self.list_frame, bg=card_bg)
        card.pack(fill="x", pady=4)              # 卡片横向填满，上下留 4 像素间距

        # 第一行：日期 + 分类胶囊 + 来源/回复数 + （如果是新消息）"● 新"标记
        top_row = tk.Frame(card, bg=card_bg)
        top_row.pack(fill="x", padx=14, pady=(8, 4))

        # 日期
        tk.Label(top_row, text=n["date"], font=("微软雅黑", 9),
                 bg=card_bg, fg=TEXT_SECOND).pack(side="left")

        # 分类胶囊（有颜色的圆角小标签）。颜色表也从注册表里取，
        # 所以新增来源时只要给它一张颜色表，卡片渲染不用改。
        colors = SOURCES[self.mode].colors
        cat_color = colors.get(n["category"], "#6B7280")   # 找不到分类颜色就用灰色
        CategoryChip(top_row, n["category"], color=cat_color,
                     bg_parent=card_bg).pack(side="left", padx=8)

        # 来源信息：显示"来源：xxx"，后面再跟上该来源自己的附加信息（meta）
        # 比如 CC98 是"回复 12 / 浏览 340"，浙大新闻的 meta 是空字符串。
        if n["source"]:
            extra = f"来源：{n['source']}"
            if n.get("meta"):
                extra += f"   {n['meta']}"
            tk.Label(top_row, text=extra,
                     font=("微软雅黑", 8), bg=card_bg, fg=TEXT_SECOND
                     ).pack(side="left", padx=4)

        # 新消息标记：红色圆点 + "新"字，靠右显示
        if is_new:
            tk.Label(top_row, text="● 新", font=("微软雅黑", 9, "bold"),
                     bg=card_bg, fg=ACCENT_RED).pack(side="right")

        # 第二行：标题（粗体，超长自动换行）
        tk.Label(card, text=n["title"], font=("微软雅黑", 11, "bold"),
                 bg=card_bg, fg=TEXT_PRIMARY, anchor="w", justify="left",
                 wraplength=760).pack(fill="x", padx=14, pady=(0, 4))

        # 第三行：摘要（超过 140 个字符就截断，加省略号）
        if n["summary"]:
            s = n["summary"]
            if len(s) > 140:
                s = s[:140] + "…"
            tk.Label(card, text=s, font=("微软雅黑", 9),
                     bg=card_bg, fg=TEXT_SECOND, anchor="w", justify="left",
                     wraplength=760).pack(fill="x", padx=14, pady=(0, 4))

        # 第四行：消息链接 + "打开"按钮
        link_row = tk.Frame(card, bg=card_bg)
        link_row.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(link_row, text=n["href"], font=("微软雅黑", 8),
                 bg=card_bg, fg=ACCENT_BLUE, anchor="w",
                 wraplength=760).pack(side="left", fill="x", expand=True)
        # "打开"按钮：点击后用系统默认浏览器打开链接
        # 注意：用 u=n["href"] 绑定默认值，避免循环里 lambda 的闭包陷阱
        RoundedButton(link_row, "打开", command=lambda u=n["href"]: self.open_url(u),
                      width=55, height=24, bg=ACCENT_BLUE,
                      font=("微软雅黑", 8), bg_parent=card_bg
                      ).pack(side="right")

    def open_url(self, url):
        """用系统默认浏览器打开指定网址。"""
        import webbrowser    # webbrowser 是标准库，负责调用系统浏览器
        webbrowser.open(url)

    # ------------------------------------------------------------------ 交互
    def switch_view(self, view):
        """切换视图（浙大新闻 / CC98论坛）：切换后重建界面并更新标题。"""
        self.mode = view
        self.build_main()
        self._update_title()

    def select_category(self, cat):
        """选择分类筛选：更新当前视图的分类并重建界面。"""
        self.data[self.mode]["category"] = cat
        self.build_main()

    def manual_refresh(self):
        """手动刷新：在后台线程执行抓取任务，避免阻塞界面。"""
        threading.Thread(target=self._fetch_task, daemon=True).start()

    def _on_interval_change(self):
        """轮询间隔输入框内容变化时调用：解析数字并更新间隔（限制在 1~180 分钟）。"""
        try:
            v = int(self.interval_var.get())     # 把输入框文字转成整数
            if 1 <= v <= 180:
                self.interval_sec = v * 60       # 换算成秒
                self.status_label.config(text=self._status_text())  # 更新状态栏显示
        except ValueError:
            pass    # 输入的不是数字就忽略

    def clear_new_flags(self):
        """清空当前视图的"新消息"标记，并刷新界面和标题。"""
        self.data[self.mode]["new_hrefs"].clear()
        self._update_title()
        self._render_list()

    # ------------------------------------------------------------------ 后台线程
    def _start_worker(self):
        """启动后台轮询线程。daemon=True 表示守护线程：主程序退出时它会自动结束。"""
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def _worker_loop(self):
        """后台线程的主循环：每隔 interval_sec 秒执行一次抓取任务。

        为什么不用 time.sleep(interval_sec) 一次睡很久？
        因为那样中途想退出程序时，线程要睡完才能响应。
        所以改成"每睡 1 秒检查一次 running 标志"，能快速响应关闭窗口。
        """
        # 稍等片刻，确保 Tk 主循环（mainloop）已启动（否则 root.after 可能报错）
        time.sleep(0.5)
        while self.running:
            self._fetch_task()
            # 按间隔等待，但每秒检查 running，便于快速退出
            for _ in range(self.interval_sec):
                if not self.running:
                    return
                time.sleep(1)

    def _fetch_task(self):
        """后台抓取任务：抓取两个来源 → 和上次对比找出新消息 → 更新界面。

        整个过程在后台线程里运行，界面相关的更新通过 self.root.after(0, ...)
        丢回主线程执行（tkinter 不允许在子线程里直接操作界面）。
        """
        new_all = {}
        for view in SOURCES:
            # 读取"已经看过"的消息链接（上次保存的）
            old_seen = self._load_seen(view)
            # 抓取最新数据（失败则跳过这个来源）
            ok = self._do_fetch(view, show_error=True)
            if not ok:
                continue

            # 检测新消息：当前抓到的链接集合 - 已经看过的链接集合 = 本次新增的
            cur_hrefs = {n["href"] for n in self.data[view]["all_news"]}
            new_hrefs = cur_hrefs - old_seen

            # 合并新旧链接并保存，供下次对比
            self._save_seen(view, old_seen | cur_hrefs)
            new_all[view] = new_hrefs

            # 把本次发现的新消息累加到界面标记里（卡片会变亮 + 显示"● 新"）
            if new_hrefs:
                self.data[view]["new_hrefs"] |= new_hrefs

        self.last_check_time = datetime.now()   # 记录本次检查时间
        # 回到主线程执行界面更新和提醒（after(0, ...) 表示尽快在主线程执行）
        self.root.after(0, lambda: self._after_fetch(new_all))

    def _after_fetch(self, new_all):
        """主线程中执行：刷新界面 + 如果发现新消息就提醒用户。"""
        self.status_label.config(text=self._status_text())   # 更新"上次检查时间"
        self._render_list()                                  # 重新渲染消息列表
        self._update_title()                                 # 更新窗口标题

        total = sum(len(v) for v in new_all.values())        # 两个来源新增消息总数
        if total:
            self.root.bell()    # 播放系统提示音
            try:
                # 尝试让窗口在最前面闪烁一下（Windows 上会高亮任务栏图标引起注意）
                self.root.attributes("-topmost", True)
                self.root.after(300, lambda: self.root.attributes("-topmost", False))
            except Exception:
                pass

            # 弹窗提醒：统计每个来源的新消息数量，并取第一条消息的标题作为示例
            parts = []
            for view in SOURCES:
                hs = new_all.get(view, set())
                if hs:
                    first = next((n for n in self.data[view]["all_news"]
                                  if n["href"] in hs), None)
                    tip = f"{SOURCES[view].label} {len(hs)} 条"
                    if first:
                        tip += f"：{first['title']}"
                    parts.append(tip)
            tip = "检测到新消息！\n\n" + "\n".join(parts)
            tip += "\n\n（窗口内新消息已用红色「● 新」标记，可点击「清空新消息标记」消除）"
            messagebox.showinfo("新消息提醒", tip, parent=self.root)

    def _update_title(self):
        """更新窗口标题：有新消息时显示 [N 条新消息]，否则只显示视图标题。"""
        total = sum(len(d["new_hrefs"]) for d in self.data.values())
        base = SOURCES[self.mode].title
        if total > 0:
            self.root.title(f"[{total} 条新消息] {base}")
        else:
            self.root.title(base)

    # ------------------------------------------------------------------ 关闭
    def on_close(self):
        """窗口关闭时调用：停止后台线程循环，然后销毁窗口。"""
        self.running = False     # 让后台线程的 while 循环退出
        self.root.destroy()      # 关闭窗口


if __name__ == "__main__":
    # 程序入口：只有直接运行本文件时才执行下面的代码
    # （如果被其他文件 import，则不会执行，因为 __name__ 不是 "__main__"）
    root = tk.Tk()               # 创建主窗口
    app = NewsMonitorApp(root)   # 创建应用实例（构造函数里会抓数据、搭界面、开线程）
    root.mainloop()              # 进入 tkinter 事件循环：程序在此等待并响应用户操作
