# -*- coding: utf-8 -*-
"""
CC98 论坛爬虫（官方只读 REST API）
=================================

通过 CC98 官方 REST API（api.cc98.org，OIDC 密码模式认证）读取论坛板块与
话题数据。全部为只读操作，不包含任何发帖 / 回帖 / 点赞行为。

抓取范围：实习兼职、求职广场、校园信息、科研学术、学习天地、留学交流、
考研一族、新生宝典等学生相关板块。

分类规则：
- 「实习求职」「留学」「考研」按板块强映射；
- 「讲座」「活动」按标题/摘要关键词识别；
- 其余归为「其他」。

排序规则：讲座 > 实习求职 > 其他（同一优先级内按时间倒序）。

仅使用 Python 标准库。

用法示例：
    python cc98爬虫.py                  # 最近 7 天
    python cc98爬虫.py --days 3
    python cc98爬虫.py --all
    python cc98爬虫.py --new-only       # 仅输出相比上次新增的
    python cc98爬虫.py --list-boards    # 列出抓取板块
"""

import argparse
import json
import re
import sys
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
API_BASE = "https://api.cc98.org"
OIDC_BASE = "https://openid.cc98.org"
FORUM_BASE = "https://www.cc98.org/topic/"

# OIDC 客户端（参考开源项目 CC98-CLI，公开只读使用）
CLIENT_ID = "9a1fd200-8687-44b1-4c20-08d50a96e5cd"
CLIENT_SECRET = "8b53f727-08e2-4509-8857-e34bf92b27f2"

CC98_USERNAME = "dr_mas_R"
CC98_PASSWORD = "@sry1610"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 抓取的板块：id -> (板块名, 类别强映射或 None)
BOARD_SOURCES = {
    459: ("实习兼职", "实习求职"),
    235: ("求职广场", "实习求职"),
    102: ("留学交流", "留学"),
    263: ("考研一族", "考研"),
    100: ("校园信息", None),
    581: ("科研学术", None),
    68: ("学习天地", None),
    198: ("新生宝典", None),
}

# 每个板块最多抓取的话题数（控制请求量，尊重限速）
BOARD_TOPIC_LIMIT = 20

# 关键词分类：类别 -> 关键词列表（标题 ×3，摘要 ×1）
CATEGORY_KEYWORDS = {
    "讲座": [
        "讲座", "报告会", "学术报告", "讲坛", "开讲", "主讲", "宣讲会",
        "研讨会", "论坛", "沙龙", "seminar", "讲演", "名家", "名师讲",
        "公开课", "学术讲座", "名师讲堂",
    ],
    "活动": [
        "报名", "招募", "活动", "比赛", "竞赛", "运动会", "晚会", "演出",
        "招新", "纳新", "宣讲", "参观", "文化节", "嘉年华", "开幕式",
        "workshop", "工作坊", "讲座预告",
    ],
}

# 分类显示顺序（讲座、实习求职优先）
CATEGORY_ORDER = ["讲座", "实习求职", "留学", "考研", "活动", "其他"]

DATA_DIR = Path(__file__).resolve().parent
SEEN_FILE = DATA_DIR / "cc98_seen.json"
OUT_FILE = DATA_DIR / "cc98_news.json"


# ---------------------------------------------------------------------------
# HTTP 与认证
# ---------------------------------------------------------------------------
class Cc98Client:
    """CC98 官方 API 客户端（OIDC 密码模式）。"""

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.access_token = None
        self.token_expire = 0
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def _request(self, url, method="GET", body=None, headers=None, timeout=20):
        h = dict(HEADERS)
        h["Accept"] = "application/json"
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode("utf-8")
            h["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def login(self):
        """密码模式获取 access token。"""
        s, raw = self._request(
            OIDC_BASE + "/connect/token", "POST",
            {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "cc98-api openid offline_access",
            },
        )
        if s != 200:
            raise RuntimeError(f"CC98 登录失败 HTTP {s}: {raw.decode('utf-8', 'ignore')[:200]}")
        obj = json.loads(raw.decode("utf-8"))
        self.access_token = obj["access_token"]
        # 默认有效期 1 小时，留 5 分钟余量
        self.token_expire = datetime.now().timestamp() + 3300
        return self.access_token

    def _ensure_token(self):
        if not self.access_token or datetime.now().timestamp() >= self.token_expire:
            self.login()

    def api(self, path, timeout=20):
        """带 token 的 GET 请求，自动处理过期重登。"""
        for _ in range(2):
            self._ensure_token()
            s, raw = self._request(
                API_BASE + path,
                headers={"Authorization": "Bearer " + self.access_token},
                timeout=timeout,
            )
            if s == 200:
                return json.loads(raw.decode("utf-8"))
            if s == 401:
                self.access_token = None
                continue
            raise RuntimeError(f"CC98 接口错误 HTTP {s}: {path} {raw.decode('utf-8', 'ignore')[:200]}")
        raise RuntimeError("CC98 认证失败（两次 401）")

    def get_board_topics(self, board_id, from_=0, size=BOARD_TOPIC_LIMIT):
        return self.api(f"/board/{board_id}/topic?from={from_}&size={size}")


# ---------------------------------------------------------------------------
# 文本清洗与分类
# ---------------------------------------------------------------------------
def clean_ubb(text: str) -> str:
    """去除 UBB 标签，返回纯文本。"""
    if not text:
        return ""
    t = re.sub(r"\[url=.*?\](.*?)\[/url\]", r"\1", text, flags=re.S)
    t = re.sub(r"\[(?:img|image|media|flash|audio|video)=?.*?\]", " ", t, flags=re.S)
    t = re.sub(r"\[/?(?:b|i|u|s|color|size|font|align|quote|list|\*|code|hide|spoiler)"
               r"(?:=[^\]]*)?\]", "", t, flags=re.I)
    t = re.sub(r"\[(?:img|image|media|flash|audio|video)(?:=[^\]]*)?\]", " ", t, flags=re.S)
    t = re.sub(r"\[/(?:img|image|media|flash|audio|video)\]", " ", t, flags=re.I)
    t = re.sub(r"\[quote\]|\[/quote\]", " ", t, flags=re.I)
    t = re.sub(r"\[\*\]", "· ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def classify_topic(title: str, summary: str, board_cat) -> str:
    """分类：板块强映射优先，否则关键词加权。"""
    if board_cat:
        return board_cat
    scores = {}
    for cat, kws in CATEGORY_KEYWORDS.items():
        s = 0
        for kw in kws:
            if kw.lower() in title.lower():
                s += 3
            if kw.lower() in summary.lower():
                s += 1
        if s > 0:
            scores[cat] = s
    if not scores:
        return "其他"
    return max(scores, key=lambda k: scores[k])


# ---------------------------------------------------------------------------
# 抓取与规整
# ---------------------------------------------------------------------------
def parse_time(s: str):
    """解析 ISO 时间，返回日期对象；失败返回 None。"""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def build_items(client: Cc98Client) -> list:
    """抓取所有配置板块并规整为统一结构的消息列表。"""
    items = []
    for board_id, (board_name, board_cat) in BOARD_SOURCES.items():
        try:
            topics = client.get_board_topics(board_id)
        except Exception:
            continue
        for t in topics or []:
            tid = t.get("id")
            title = (t.get("title") or "").strip()
            if not tid or not title:
                continue
            dt = parse_time(t.get("time"))
            if not dt:
                continue
            summary = clean_ubb(t.get("lastPostContent") or "")
            category = classify_topic(title, summary, board_cat)
            items.append(
                {
                    "title": title,
                    "date": dt.strftime("%Y-%m-%d"),
                    "href": FORUM_BASE + str(tid),
                    "summary": summary[:300],
                    "source": board_name,
                    "category": category,
                    "reply": t.get("replyCount", 0),
                    "hit": t.get("hitCount", 0),
                }
            )
    return items


def sort_items(items: list) -> list:
    """排序：讲座 > 实习求职 > 其他（优先级升序），同级内按日期倒序。"""
    def rank(n):
        try:
            return CATEGORY_ORDER.index(n["category"])
        except ValueError:
            return len(CATEGORY_ORDER)
    # 先按日期倒序（稳定排序），再按优先级升序（稳定排序保持日期顺序）
    items.sort(key=lambda n: n["date"], reverse=True)
    items.sort(key=rank)
    return items


def filter_recent(items: list, days: int) -> list:
    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    return [n for n in items if datetime.strptime(n["date"], "%Y-%m-%d") >= cutoff]


def print_results(items: list) -> None:
    if not items:
        print("（暂无新消息）")
        return
    stat = {}
    for n in items:
        stat[n["category"]] = stat.get(n["category"], 0) + 1
    print("分类统计：", "  ".join(f"{k}({v})" for k, v in sorted(stat.items(), key=lambda x: -x[1])))
    print("-" * 60)
    for n in items:
        print(f"[{n['date']}] [{n['category']}] {n['title']}")
        print(f"  来源: {n['source']}  回复 {n['reply']}  浏览 {n['hit']}")
        print(f"  链接: {n['href']}")
        if n["summary"]:
            s = n["summary"]
            if len(s) > 120:
                s = s[:120] + "…"
            print(f"  摘要: {s}")
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CC98 论坛爬虫（官方只读 API）")
    ap.add_argument("--days", type=int, default=7, help="只保留最近 N 天（默认 7）")
    ap.add_argument("--all", action="store_true", help="抓取全部，忽略天数")
    ap.add_argument("--new-only", action="store_true", help="仅输出相比上次新增的")
    ap.add_argument("--output", default=str(OUT_FILE), help="JSON 输出路径")
    ap.add_argument("--seen", default=str(SEEN_FILE), help="已见链接记录路径")
    ap.add_argument("--list-boards", action="store_true", help="列出抓取板块后退出")
    args = ap.parse_args(argv)

    if args.list_boards:
        print("抓取板块：")
        for bid, (name, cat) in BOARD_SOURCES.items():
            print(f"  [{bid}] {name}" + (f" -> {cat}" if cat else ""))
        return 0

    client = Cc98Client(CC98_USERNAME, CC98_PASSWORD)
    try:
        client.login()
    except Exception as e:
        print(f"CC98 登录失败：{e}", file=sys.stderr)
        return 1

    try:
        items = build_items(client)
    except Exception as e:
        print(f"CC98 抓取失败：{e}", file=sys.stderr)
        return 2
    if not items:
        print("未能从任何板块解析到话题。", file=sys.stderr)
        return 3

    filtered = items if args.all else filter_recent(items, args.days)
    filtered = sort_items(filtered)

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
        all_hrefs = sorted(old | {n["href"] for n in items})
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
        f"共 {len(out)} 条消息（板块解析 {len(items)} 条，过滤后 {len(filtered)} 条），"
        f"已保存到 {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
