# -*- coding: utf-8 -*-
"""
浙江大学心理与行为科学系爬虫（转专业信息专项）
==============================================

用途：为准备转专业（目标：脑机智能创新班）而搜集浙大心理与行为科学系
官网（http://www.psych.zju.edu.cn/）上的信息，重点跟踪「转专业」相关通知。

网站背景：
    该站使用博达 VSB 网站群系统（页面为 UTF-8 编码的普通 HTML，不需要登录）。
    每条通知的网址形如：
        http://www.psych.zju.edu.cn/2026/0916/c27653a3204112/page.htm
        其中 2026/0916 表示发布日期，c27653 是栏目编号，3204112 是文章编号。

抓取栏目（见 COLUMNS 常量，可自行增删）：
    27653  本科生教育·最新通知  —— 所有转专业通知都发在这里（主栏目）
    27575  通知公告             —— 系级综合通知（补充栏目）

两个实用小技巧（代码中已利用）：
    1. 列表页每条消息都是固定结构：
           <a href="/2026/0916/c27653a3204112/page.htm">标题</a>
           <span class="time">2026-09-16</span>
       用一个正则表达式即可同时取出链接、标题、日期。
    2. 详情页的 <head> 里有两个 <meta name="description">：第一个是栏目分类
       （没用），第二个的 content 直接就是「整篇文章的纯文本全文」，
       所以不必去解析复杂的正文 HTML 表格，直接读 meta 即可拿到摘要。

分类规则（按标题关键词，详见 CATEGORY_KEYWORDS）：
    转专业 > 推免 > 毕业论文 > 科研训练 > 国际交流 > 教务通知 > 其他
    「转专业」类始终排在最前面，方便优先查看。

仅使用 Python 标准库，无需安装第三方依赖。

用法示例：
    python 心理系爬虫.py                     # 默认：近 365 天，抓每个栏目第 1 页并补全正文摘要
    python 心理系爬虫.py --transfer-only     # 只看转专业相关通知
    python 心理系爬虫.py --pages 3           # 每个栏目抓 3 页（每页 20 条）
    python 心理系爬虫.py --no-content        # 只抓列表，不进详情页（速度快、请求少）
    python 心理系爬虫.py --keyword 脑机      # 标题/正文中搜索关键词
    python 心理系爬虫.py --days 30           # 只看最近 30 天
    python 心理系爬虫.py --all               # 不限日期
    python 心理系爬虫.py --new-only          # 仅输出相比上次运行新增的链接
    python 心理系爬虫.py --list-columns      # 列出抓取栏目后退出
"""

import argparse
import html as html_lib          # 用来把 &amp; &nbsp; 这类 HTML 转义还原成普通文字
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
BASE_URL = "http://www.psych.zju.edu.cn"

# 请求头：伪装成普通浏览器，避免被网站的简单反爬策略拦截
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 抓取栏目：栏目编号 -> 栏目显示名
# 转专业通知固定发在 27653；27575 作为补充。想增减栏目只改这里即可。
COLUMNS = {
    27653: "本科生教育·最新通知",
    27575: "通知公告",
}

# 默认每个栏目抓几页列表（每页 20 条）。第 1 页通常已覆盖最近一个学期。
DEFAULT_PAGES = 1

# 最多进入多少个详情页补全正文（详情页请求量较大，设上限以示礼貌）。
# 列表按日期从新到旧排列，所以补全的永远是最新的若干条。
DETAIL_LIMIT = 25

# 每发起一次网络请求后暂停的秒数（避免请求过快给网站造成压力）
POLITE_SLEEP = 0.3

# 默认保留最近多少天的通知：转专业以学年为周期，设 365 天可看到完整一轮
DEFAULT_DAYS = 365

# ---------------------------------------------------------------------------
# 分类规则
# ---------------------------------------------------------------------------
# 分类 -> 该类标题中常见的关键词（统一转小写后匹配，不区分大小写）。
# 判断时严格按下面 CATEGORY_ORDER 的顺序，第一个命中的类别即为结果：
# 这样能避免一条通知同时命中多个词时归类含糊（顺序经过人工安排）。
CATEGORY_KEYWORDS = {
    "转专业": ["转专业", "转系", "转入", "转出"],
    "推免": ["推免", "免试", "推荐免试", "综合排名", "拟推荐"],
    "毕业论文": ["毕业论文", "答辩", "毕业证", "学位证", "结业证"],
    "科研训练": ["创新训练计划", "srtp", "立项", "结题", "科研训练"],
    "国际交流": ["访学", "暑期学校", "summer school", "交流项目", "牛津", "kings college"],
    "教务通知": ["绩点", "教材", "课程", "选课", "辅修", "成绩"],
}

# 分类显示顺序：「转专业」最重要排最前；未命中任何关键词的归为「其他」
CATEGORY_ORDER = ["转专业", "推免", "毕业论文", "科研训练", "国际交流", "教务通知", "其他"]

# 数据文件（与 cc98爬虫.py 风格保持一致：一个结果文件 + 一个去重记录文件）
DATA_DIR = Path(__file__).resolve().parent
SEEN_FILE = DATA_DIR / "psych_seen.json"
OUT_FILE = DATA_DIR / "psych_news.json"


# ---------------------------------------------------------------------------
# 网页请求
# ---------------------------------------------------------------------------
def fetch(url: str, timeout: int = 15) -> str:
    """请求一个网址，返回解码后的 HTML 文本（UTF-8）。

    失败时抛出异常，由调用方决定如何处理（通常是跳过这一页继续）。
    """
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="ignore")


def list_page_url(column_id: int, page: int) -> str:
    """拼出某栏目某一页的网址。

    博达 VSB 的分页规则：第 1 页是 list.htm，第 2 页起是 list2.htm、list3.htm……
    """
    if page <= 1:
        return f"{BASE_URL}/{column_id}/list.htm"
    return f"{BASE_URL}/{column_id}/list{page}.htm"


# ---------------------------------------------------------------------------
# 列表页解析
# ---------------------------------------------------------------------------
def parse_list(html: str) -> list:
    """从栏目列表页 HTML 中解析出通知条目。

    返回若干原始字典：{href, title, date, column_id}。

    正则匹配的目标结构（同一行内）：
        <a href="/2026/0916/c27653a3204112/page.htm" target="_blank">标题文字</a>
        <span class="time">2026-09-16</span>
    """
    items = []
    pattern = re.compile(
        r'<a[^>]+href="(/[^"]*?c(?P<col>\d+)a\d+/page\.htm)"[^>]*>'
        r'(?P<title>.*?)</a>\s*<span class="time">(?P<date>\d{4}-\d{2}-\d{2})</span>',
        re.S,
    )
    for m in pattern.finditer(html):
        title = re.sub(r"<[^>]+>", "", m.group("title"))   # 标题里若夹标签则去掉
        title = html_lib.unescape(title).strip()
        if not title:
            continue
        items.append(
            {
                "href": BASE_URL + m.group(1),
                "title": title,
                "date": m.group("date"),
                "column_id": int(m.group("col")),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 详情页解析（提取纯文本全文作为摘要）
# ---------------------------------------------------------------------------
def parse_detail(html: str) -> str:
    """从详情页提取正文纯文本。

    页面里有多个 <meta name="description">，取内容最长的那个 ——
    短的是栏目分类（如「党政管理」），只有最长的才是文章全文。
    """
    candidates = re.findall(
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
        html,
        flags=re.S | re.I,
    )
    if not candidates:
        return ""
    text = max(candidates, key=len)
    return html_lib.unescape(text).strip()


# 知识库支持解析的附件扩展名（博达网站群附件都放在 /_upload/ 路径下）
ATTACHMENT_EXTS = ("pdf", "docx", "doc", "xlsx", "xls")


def parse_attachments(html: str) -> list:
    """从详情页 HTML 中提取附件链接。

    网站附件链接的真实形态（2026-09 确认）：
        <a href="/_upload/article/files/cd/7d/9ae.../xxx.docx">
            【附件】攻读博士学位研究生专家推荐信模版.docx
        </a>
    返回若干字典：{filename, url, ext}（相对路径已补全为绝对链接，按链接去重）。
    """
    out = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, inner = m.group(1), m.group(2)
        # 必须是 _upload 路径且扩展名受支持，避免把普通文章链接误判为附件
        if "_upload" not in href:
            continue
        mm = re.search(r"\.(" + "|".join(ATTACHMENT_EXTS) + r")(?:[?#]|$)",
                       href, re.I)
        if not mm:
            continue
        ext = mm.group(1).lower()
        url = urllib.parse.urljoin(BASE_URL + "/", href)
        name = re.sub(r"<[^>]+>", "", inner)            # 去掉链接文字里的嵌套标签
        name = html_lib.unescape(name).strip()
        if not name:                                     # 链接文字为空时用 URL 末段当文件名
            name = url.rsplit("/", 1)[-1]
        if url in seen:
            continue
        seen.add(url)
        out.append({"filename": name, "url": url, "ext": ext})
    return out


def enrich_with_content(candidates: list) -> None:
    """按给定顺序给至多 DETAIL_LIMIT 条通知就地补上正文字段。

    「补全谁、先后顺序」由调用方排好后传入：本爬虫会把转专业类通知排在
    最前面，保证即使名额用完，每一条转专业通知也都能拿到正文。
    其余条目的 summary 保持为空字符串（列表页本身不含摘要）。
    任何一条详情页请求失败都不影响整体流程，只是该条没有摘要。
    """
    for n in candidates[:DETAIL_LIMIT]:
        try:
            page = fetch(n["href"])
            time.sleep(POLITE_SLEEP)          # 礼貌间隔
            n["summary"] = parse_detail(page)
            n["attachments"] = parse_attachments(page)   # 同一页 HTML 里顺带提取附件
        except (urllib.error.URLError, TimeoutError, OSError):
            n["summary"] = ""
            n["attachments"] = []


# ---------------------------------------------------------------------------
# 分类与排序
# ---------------------------------------------------------------------------
def classify(title: str) -> str:
    """按标题关键词判断通知类别；全部不命中时归为「其他」。"""
    t = title.lower()
    for cat in CATEGORY_ORDER[:-1]:            # 依次尝试除「其他」外的所有类别
        if any(kw in t for kw in CATEGORY_KEYWORDS[cat]):
            return cat
    return "其他"


def sort_items(items: list) -> list:
    """排序：转专业等类别按 CATEGORY_ORDER 优先，同一类别内日期从新到旧。

    利用 Python 排序的稳定性：先按日期倒序排好，再按类别优先级稳定排序，
    就能在保持类别顺序的同时，让每个类别内部维持日期倒序。
    """
    def rank(n):
        try:
            return CATEGORY_ORDER.index(n["category"])
        except ValueError:
            return len(CATEGORY_ORDER)

    items.sort(key=lambda n: n["date"], reverse=True)
    items.sort(key=rank)
    return items


# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------
def filter_recent(items: list, days: int) -> list:
    """只保留最近 N 天的通知（按日期比较）。"""
    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    return [n for n in items if datetime.strptime(n["date"], "%Y-%m-%d") >= cutoff]


# ---------------------------------------------------------------------------
# 主抓取流程
# ---------------------------------------------------------------------------
def scrape(pages: int = DEFAULT_PAGES, with_content: bool = True,
           log=print) -> list:
    """执行完整抓取，返回统一格式的通知列表（已分类、排序、去重）。

    参数：
        pages        : 每个栏目抓几页
        with_content : 是否进入详情页补全正文摘要
        log          : 进度信息输出函数（监控窗口接入时可替换成静默函数）

    每条记录包含：title / date / href / summary / source / category
    """
    raw_items = []
    for column_id, column_name in COLUMNS.items():
        before = len(raw_items)
        for page in range(1, pages + 1):
            url = list_page_url(column_id, page)
            try:
                page_items = parse_list(fetch(url))
                time.sleep(POLITE_SLEEP)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                log(f"  警告：栏目 {column_id} 第 {page} 页抓取失败：{e}")
                break
            if not page_items:
                # 空页通常意味着已经翻过最后一页，不必继续
                break
            for it in page_items:
                it["source"] = column_name
            raw_items.extend(page_items)
        log(f"  栏目 {column_id}「{column_name}」得到 {len(raw_items) - before} 条")

    # 同一篇文章可能同时挂在两个栏目里，按链接去重（保留先出现的）
    seen_hrefs = set()
    unique = []
    for n in raw_items:
        if n["href"] in seen_hrefs:
            continue
        seen_hrefs.add(n["href"])
        n["summary"] = ""
        n["attachments"] = []          # 未进详情页的条目没有附件信息
        n["category"] = classify(n["title"])
        unique.append(n)

    # 详情页补全顺序：转专业类优先（全部排在前面），其余按日期从新到旧。
    # 这样可以保证最关键的转专业通知条条都有正文摘要。
    transfer = [n for n in unique if n["category"] == "转专业"]
    others = [n for n in unique if n["category"] != "转专业"]
    transfer.sort(key=lambda n: n["date"], reverse=True)
    others.sort(key=lambda n: n["date"], reverse=True)
    candidates = transfer + others
    if with_content:
        log(f"  正在进入详情页补全 {min(DETAIL_LIMIT, len(candidates))} 条正文"
            f"（转专业类 {len(transfer)} 条优先）……")
        enrich_with_content(candidates)

    return sort_items(unique)


# ---------------------------------------------------------------------------
# 结果打印
# ---------------------------------------------------------------------------
def print_results(items: list) -> None:
    """把结果按易读格式输出到控制台。"""
    if not items:
        print("（没有符合条件的通知）")
        return
    stat = {}
    for n in items:
        stat[n["category"]] = stat.get(n["category"], 0) + 1
    print("分类统计：", "  ".join(f"{k}({v})" for k, v in stat.items()))
    print("-" * 60)
    for n in items:
        print(f"[{n['date']}] [{n['category']}] {n['title']}")
        print(f"  来源：{n['source']}")
        print(f"  链接：{n['href']}")
        if n["summary"]:
            s = n["summary"]
            if len(s) > 150:
                s = s[:150] + "…"
            print(f"  摘要：{s}")
        print()


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="浙大心理与行为科学系爬虫（转专业信息专项）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"只保留最近 N 天（默认 {DEFAULT_DAYS}）")
    ap.add_argument("--all", action="store_true", help="不限日期，保留全部")
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES,
                    help=f"每个栏目抓几页列表（默认 {DEFAULT_PAGES}，每页 20 条）")
    ap.add_argument("--no-content", action="store_true",
                    help="只抓列表，不进入详情页（请求少、速度快）")
    ap.add_argument("--transfer-only", action="store_true",
                    help="只输出「转专业」类通知")
    ap.add_argument("--keyword", default="",
                    help="在标题和正文中搜索关键词（需开启正文补全）")
    ap.add_argument("--new-only", action="store_true", help="仅输出相比上次新增的链接")
    ap.add_argument("--output", default=str(OUT_FILE), help="JSON 结果输出路径")
    ap.add_argument("--seen", default=str(SEEN_FILE), help="已见链接记录路径")
    ap.add_argument("--list-columns", action="store_true", help="列出抓取栏目后退出")
    args = ap.parse_args(argv)

    if args.list_columns:
        print("抓取栏目：")
        for cid, name in COLUMNS.items():
            mark = "（转专业通知主栏目）" if cid == 27653 else ""
            print(f"  [{cid}] {name} {mark}")
        return 0

    print("开始抓取浙大心理与行为科学系官网……")
    try:
        items = scrape(pages=args.pages, with_content=not args.no_content)
    except Exception as e:
        print(f"抓取失败：{e}", file=sys.stderr)
        return 2
    if not items:
        print("未解析到任何通知（可能是网站改版或网络问题）。", file=sys.stderr)
        return 3

    # 依次套用：日期过滤 -> 转专业专项过滤 -> 关键词过滤
    filtered = items if args.all else filter_recent(items, args.days)
    if args.transfer_only:
        filtered = [n for n in filtered if n["category"] == "转专业"]
    if args.keyword:
        kw = args.keyword.lower()
        filtered = [n for n in filtered
                    if kw in n["title"].lower() or kw in (n["summary"] or "").lower()]

    # 新增检测：与本地保存的"已见链接"对比
    out = filtered
    seen_path = Path(args.seen)
    if args.new_only:
        old = set()
        if seen_path.exists():
            try:
                old = set(json.loads(seen_path.read_text(encoding="utf-8")).get("hrefs", []))
            except Exception:
                old = set()
        out = [n for n in filtered if n["href"] not in old]
        all_hrefs = sorted(old | {n["href"] for n in items})
        seen_path.write_text(
            json.dumps(
                {"hrefs": all_hrefs,
                 "updated": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    print_results(out)
    Path(args.output).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"共输出 {len(out)} 条（列表解析 {len(items)} 条，过滤后 {len(filtered)} 条），"
        f"已保存到 {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
