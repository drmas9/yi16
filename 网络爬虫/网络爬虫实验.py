# -*- coding: utf-8 -*-
"""
浙江大学官网新闻爬虫
====================

浙大官网首页（https://www.zju.edu.cn/）把各新闻栏目的内容以 JS 数据的形式
嵌入在 <script> 标签里，形如：

    main._newsJson11 = [ {
        id: '1',
        image: '...',
        title: '浙江大学党委书记任少波带队赴云南景东调研考察',
        text: JSON.stringify(`9月4日，浙江大学党委书记任少波……`),
        time: '2026-09-06',
        href: 'https://mp.weixin.qq.com/s/...',
        open: '_blank'
    }, { ... } ];

本爬虫抓取首页 HTML，解析所有 `main._newsJsonXX` 窗口中的条目，提取
标题 / 日期 / 链接 / 摘要 / 来源，按日期过滤“新消息”并输出到控制台与 JSON 文件。

同时基于关键词规则对每条新闻做内容分类（加权评分），分为 9 类：
校务管理 / 科研学术 / 人物报道 / 讲座公告 / 展览文体 / 招生就业 / 国际合作 / 社会服务 / 媒体聚焦
（关键词词典见 CATEGORIES 常量，可按需调整）。

仅使用 Python 标准库，无需安装第三方依赖。

用法示例：
    python 网络爬虫实验.py                 # 最近 7 天的新消息（含分类）
    python 网络爬虫实验.py --days 3        # 最近 3 天
    python 网络爬虫实验.py --all          # 抓取全部新闻（忽略天数）
    python 网络爬虫实验.py --new-only     # 仅输出相比上次运行新增的消息
    python 网络爬虫实验.py --list-categories   # 查看全部分类与关键词
    python 网络爬虫实验.py --category 科研学术  # 只看「科研学术」类
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


HOME_URL = "https://www.zju.edu.cn/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 首页新闻窗口编号 -> 栏目名称（尽力映射，无法识别时回退到编号）
COLUMN_MAP = {
    "11": "新闻·头条",
    "12": "新闻·列表",
    "13": "媒体浙大",
    "31": "聚焦",
    "41": "同行·人物",
    "51": "展览",
    "71": "专题",
}

# ---------------------------------------------------------------------------
# 内容分类（基于关键词规则的加权评分）
# ---------------------------------------------------------------------------
# 每个类别对应一组关键词；关键词在标题中命中权重 ×3，在摘要中命中 ×1。
# 取累计得分最高者作为主分类；全部为 0 时归为「其他」。
# 词典集中维护，可按需增删关键词调整分类效果。
CATEGORIES = {
    "校务管理": [
        "党委书记", "校长", "常委", "常委会", "党委", "工作会议", "总结会",
        "政绩观", "党建", "廉政", "从严治党", "师德师风", "全会", "党委常委会",
        "意识形态", "巡视", "纪检", "组织生活", "学习教育",
    ],
    "科研学术": [
        "《自然》", "《科学》", "Nature", "Science", "Cell", "论文", "研究成果",
        "研究突破", "突破", "发表", "科学技术奖", "国家科技", "专利", "实验室",
        "创新团队", "重点实验室", "显微镜", "材料", "基因", "细胞", "量子",
        "人工智能", "算法", "化合物", "新材料", "新成果", "科研", "学术",
    ],
    "人物报道": [
        "教授", "院士", "学者", "校友", "人物", "先生", "她", "获奖者",
        "先进个人", "师德", "名师", "榜样", "专访", "记", "人物志",
    ],
    "讲座公告": [
        "讲座", "大讲堂", "论坛", "报告会", "学术报告", "讲堂", "研讨会",
        "讲座通知", "开讲", "主讲", "海外名师", "讲坛", "前沿讲坛", "科学前沿",
    ],
    "展览文体": [
        "展览", "博物馆", "艺术展", "展", "画展", "宋韵", "唐卡", "绘画大系",
        "手稿", "档案", "音乐会", "乐团", "越剧", "戏剧", "话剧", "演出",
        "三好杯", "运动会", "体育舞蹈", "操舞", "比赛", "水上运动会",
        "舞蹈", "琴", "筝", "嘉年华", "节",
    ],
    "招生就业": [
        "招生", "新生", "录取", "就业", "招聘", "毕业", "考研", "强基计划",
        "本科", "硕士", "博士", "招生简章", "双选会", "宣讲会", "迎新",
        "开学典礼", "毕业典礼",
    ],
    "国际合作": [
        "国际", "全球", "交流", "出访", "访学", "共建", "一带一路", "海外",
        "外宾", "世界", "中外", "合作办学", "国际会议", "学者来访",
    ],
    "社会服务": [
        "乡村振兴", "帮扶", "扶贫", "支教", "社会服务", "定点帮扶", "地方合作",
        "市校合作", "校地合作", "科技成果转化", "驻村", "对口支援",
    ],
    "媒体聚焦": [
        "人民日报", "光明日报", "科技日报", "学习时报", "新华社", "央视", "CCTV",
        "新华网", "人民网", "光明网", "媒体报道", "媒体",
    ],
}

# 栏目 -> 类别的强映射（栏目信号比关键词更可靠时使用）
COLUMN_CATEGORY = {
    "媒体浙大": "媒体聚焦",
    "同行·人物": "人物报道",
}


def classify(item: dict) -> str:
    """根据标题/摘要/栏目/来源做关键词加权评分，返回主分类名称。"""
    # 1) 栏目强映射优先
    col = item.get("column", "")
    if col in COLUMN_CATEGORY:
        return COLUMN_CATEGORY[col]

    title = item.get("title", "")
    summary = item.get("summary", "") or ""
    source = item.get("source", "") or ""

    scores = {}
    for cat, kws in CATEGORIES.items():
        s = 0
        for kw in kws:
            if kw in title:
                s += 3
            if kw in summary:
                s += 1
            if kw in source:
                s += 2
        if s > 0:
            scores[cat] = s

    # 无任何命中：若来源非空且像媒体名，归「媒体聚焦」，否则「其他」
    if not scores:
        if source and any(m in source for m in ("日报", "时报", "新华社", "电视", "网")):
            return "媒体聚焦"
        return "其他"

    # 取最高分；并列时按词典定义顺序取首个（稳定）
    best = max(scores, key=lambda k: scores[k])
    return best


def fetch(url: str, timeout: int = 15) -> str:
    """获取页面 HTML 文本。"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    # 浙大官网为 UTF-8
    return data.decode("utf-8", errors="ignore")


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else ""


def parse_items(body: str) -> list:
    """解析单个 _newsJson 数组体中的多个新闻条目。

    以 `title: '...'` 为锚点切分条目，避免条目正文中出现 { } 造成误切。
    """
    items = []
    titles = list(re.finditer(r"title:\s*'((?:[^'\\]|\\.)*)'", body))
    for i, tm in enumerate(titles):
        start = tm.end()
        end = titles[i + 1].start() if i + 1 < len(titles) else len(body)
        seg = body[start:end]
        items.append(
            {
                "title": tm.group(1).strip(),
                "time": _first(r"time:\s*'([^']*)'", seg).strip(),
                "href": _first(r"href:\s*'([^']*)'", seg).strip(),
                "summary": _first(r"text:\s*JSON\.stringify\(`([^`]*)`\)", seg).strip(),
                "source": _first(r"info:\s*'([^']*)'", seg).strip(),
            }
        )
    return items


def parse_home(html: str) -> list:
    """从首页 HTML 中提取所有 _newsJson 窗口的新闻条目。"""
    items = []
    for m in re.finditer(r"main\._newsJson(\d+)\s*=\s*\[(.*?)\];", html, re.S):
        win = m.group(1)
        for it in parse_items(m.group(2)):
            it["window"] = win
            it["column"] = COLUMN_MAP.get(win, f"窗口{win}")
            items.append(it)
    return items


def to_absolute(href: str) -> str:
    """将相对链接补全为绝对链接。"""
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.zju.edu.cn" + href
    return "https://www.zju.edu.cn/" + href


def parse_date(s: str):
    """解析多种日期格式，失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def is_news_link(href: str) -> bool:
    """判断链接是否为一条新闻，而非栏目入口/图片等。"""
    if not href:
        return False
    low = href.lower()
    if low.endswith(("/list.htm", "/listm.htm")):
        return False
    if low.endswith((".jpg", ".png", ".gif", ".jpeg")):
        return False
    return True


def build_news(raw: list) -> list:
    """规整 + 去重 + 补全，返回结构化新闻列表。"""
    seen = set()
    news = []
    for it in raw:
        href = to_absolute(it.get("href", ""))
        if not is_news_link(href) or href in seen:
            continue
        seen.add(href)
        dt = parse_date(it.get("time", ""))
        if not dt:  # 新闻一定有日期，跳过无日期的静态条目
            continue
        record = {
            "title": it.get("title", ""),
            "date": dt.strftime("%Y-%m-%d"),
            "href": href,
            "summary": it.get("summary", ""),
            "source": it.get("source", ""),
            "column": it.get("column", ""),
        }
        record["category"] = classify(record)
        news.append(record)
    return news


def filter_recent(news: list, days: int) -> list:
    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    return [n for n in news if datetime.strptime(n["date"], "%Y-%m-%d") >= cutoff]


def print_results(items: list) -> None:
    if not items:
        print("（暂无新消息）")
        return

    # 分类统计
    stat = {}
    for n in items:
        stat[n["category"]] = stat.get(n["category"], 0) + 1
    print("分类统计：", "  ".join(f"{k}({v})" for k, v in sorted(stat.items(), key=lambda x: -x[1])))
    print("-" * 60)

    for n in items:
        print(f"[{n['date']}] [{n['category']}] {n['title']}")
        tags = [n["column"]] if n["column"] else []
        if n["source"]:
            tags.append(n["source"])
        if tags:
            print(f"  栏目: {' / '.join(tags)}")
        print(f"  链接: {n['href']}")
        if n["summary"]:
            s = n["summary"]
            if len(s) > 120:
                s = s[:120] + "…"
            print(f"  摘要: {s}")
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="浙江大学官网新闻爬虫")
    ap.add_argument("--url", default=HOME_URL, help=f"抓取入口（默认 {HOME_URL}）")
    ap.add_argument("--days", type=int, default=7, help="只保留最近 N 天的消息（默认 7）")
    ap.add_argument("--all", action="store_true", help="抓取全部新闻，忽略天数过滤")
    ap.add_argument(
        "--new-only",
        action="store_true",
        help="仅输出相比上次运行新增的消息（基于本地 zju_seen.json 记录）",
    )
    ap.add_argument("--output", default="zju_news.json", help="JSON 结果输出路径")
    ap.add_argument("--seen", default="zju_seen.json", help="已见链接记录路径")
    ap.add_argument(
        "--category",
        default=None,
        help="只输出指定分类的消息（如：科研学术、人物报道）；可用 --list-categories 查看全部",
    )
    ap.add_argument(
        "--list-categories",
        action="store_true",
        help="列出全部内容分类后退出",
    )
    args = ap.parse_args(argv)

    if args.list_categories:
        print("内容分类：")
        for cat, kws in CATEGORIES.items():
            print(f"  {cat}（关键词示例：{', '.join(kws[:6])}…）")
        return 0

    try:
        html = fetch(args.url)
    except Exception as e:
        print(f"抓取首页失败：{e}", file=sys.stderr)
        return 1

    raw = parse_home(html)
    news = build_news(raw)
    if not news:
        print("未能从首页解析到任何新闻条目，请检查页面结构是否变化。", file=sys.stderr)
        return 2

    filtered = news if args.all else filter_recent(news, args.days)
    if args.category:
        filtered = [n for n in filtered if n["category"] == args.category]
    filtered.sort(key=lambda n: n["date"], reverse=True)

    out = filtered
    if args.new_only:
        seen_path = Path(args.seen)
        old = set()
        if seen_path.exists():
            try:
                old = set(json.loads(seen_path.read_text(encoding="utf-8")).get("hrefs", []))
            except Exception:
                old = set()
        out = [n for n in filtered if n["href"] not in old]
        all_hrefs = sorted(old | {n["href"] for n in news})
        seen_path.write_text(
            json.dumps(
                {"hrefs": all_hrefs, "updated": datetime.now().isoformat(timespec="seconds")},
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
        f"共 {len(out)} 条消息（首页解析到 {len(news)} 条，"
        f"过滤后 {len(filtered)} 条），已保存到 {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
