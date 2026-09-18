# -*- coding: utf-8 -*-
"""
B 站转专业经验视频采集（P3：非官方经验，第二来源）
==============================================================
B 站搜索接口需要 wbi 签名（官方未公开但算法公开），这里只用 Python
标准库实现：访问首页拿 Cookie → 调 nav 接口拿密钥 → 参数签名后搜索。
命中的视频再尝试抓取简介与字幕（字幕需视频自带，很多视频没有，
没有就只存标题和简介，不会失败），写入「心理系知识库」，
来源 bilibili、分类「经验帖」。

B 站搜索是宽泛相关排序（搜"心理系转专业"会出来其他学校、考研视频），
所以客户端做了严格过滤：
    规则1：标题或简介含"心理/脑机"且与"浙大/转专业"相关；
    规则2：讲浙江大学转专业的通用经验（不含心理也收，最多 N 条）。

用法：
    python -B B站经验采集.py              # 搜索 + 字幕 + 入库
    python -B B站经验采集.py --dry-run    # 只打印命中，不写库
    python -B B站经验采集.py --no-subtitle  # 不抓字幕，速度更快
"""

import argparse
import hashlib
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import 心理系知识库 as kb                    # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 搜索词：围绕心理系、目标班级与浙大转专业通用经验
SEARCH_KEYWORDS = ["心理系转专业", "脑机智能创新班", "浙大转专业心理",
                   "浙江大学 心理 转专业"]
SEARCH_SIZE = 20
SEARCH_SLEEP = 2.5          # 搜索请求间隔
REQUEST_SLEEP = 1.0         # 其他接口请求间隔
VIEW_412_SLEEP = 15         # 详情接口被风控(412)后的退避秒数
MAX_VIEW_FAILURES = 3       # 详情连续失败这么多次就放弃本轮（风控窗口）
MAX_GENERAL_ZJU = 10        # 规则2（浙大通用转专业经验）最多收录条数；
                            # B站排序每次略有漂移，限额太大会导致边缘视频
                            # 分次入库，适当放宽一次收全，之后运行即稳定
MAX_SUBTITLE_CHARS = 20000  # 字幕入库上限

# wbi 密钥混淆表（B 站前端公开算法中的固定表）
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


# ---------------------------------------------------------------------------
# 带 Cookie 与 wbi 签名的客户端
# ---------------------------------------------------------------------------
class BiliClient:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = [("User-Agent", UA),
                                  ("Referer", "https://www.bilibili.com/")]
        self._mixin = None

    def _get_json(self, url, timeout=15):
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))

    def prepare(self):
        """访问首页拿 buvid Cookie，再从 nav 接口取 wbi 密钥。"""
        self.opener.open("https://www.bilibili.com/", timeout=15).read()[:0]
        nav = self._get_json("https://api.bilibili.com/x/web-interface/nav")
        wbi = nav.get("data", {}).get("wbi_img", {})
        img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        self._mixin = "".join(raw[i] for i in _MIXIN_TAB)[:32]

    def _signed(self, params: dict) -> dict:
        """给查询参数补上 wts 与 w_rid 签名。"""
        params = dict(params)
        params["wts"] = int(time.time())
        # 按 key 排序后做 urlencode（与前端签名规则一致）
        qs = urllib.parse.urlencode(sorted(params.items()))
        params["w_rid"] = hashlib.md5((qs + self._mixin).encode()).hexdigest()
        return params

    def search(self, keyword: str) -> list:
        params = self._signed({
            "search_type": "video", "keyword": keyword,
            "page": 1, "page_size": SEARCH_SIZE, "order": "totalrank",
        })
        url = ("https://api.bilibili.com/x/web-interface/wbi/search/type?"
               + urllib.parse.urlencode(params))
        obj = self._get_json(url)
        if obj.get("code") != 0:
            raise RuntimeError(f"搜索失败 code={obj.get('code')} "
                               f"{obj.get('message', '')[:80]}")
        return (obj.get("data") or {}).get("result") or []

    def get_view(self, bvid: str) -> dict:
        """视频详情接口：返回的标题/简介/UP主/发布时间是稳定的，
        不随搜索词变化（搜索接口的 description 会因命中关键词不同
        而在"全文 / '-' / 截断"之间漂移，不能直接入库）。"""
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        obj = self._get_json(url)
        if obj.get("code") != 0:
            raise RuntimeError(f"详情失败 code={obj.get('code')}")
        return obj.get("data") or {}

    def get_pages(self, bvid: str) -> list:
        """取视频分 P 列表（需要 cid 才能拿字幕）。"""
        url = (f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}")
        obj = self._get_json(url)
        return obj.get("data") or []

    def get_subtitle_urls(self, bvid: str, cid: int) -> list:
        """取字幕文件地址列表（AI 字幕/UP 主自制字幕）。"""
        params = self._signed({"bvid": bvid, "cid": cid})
        url = ("https://api.bilibili.com/x/player/wbi/v2?"
               + urllib.parse.urlencode(params))
        obj = self._get_json(url)
        subs = (((obj.get("data") or {}).get("subtitle") or {})
                .get("subtitles")) or []
        return [s["subtitle_url"] for s in subs if s.get("subtitle_url")]

    def get_subtitle_text(self, subtitle_url: str) -> str:
        """下载字幕 JSON，拼成纯文本。"""
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        obj = self._get_json(subtitle_url)
        lines = [seg.get("content", "") for seg in obj.get("body", [])]
        return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------------------
# 相关性过滤
# ---------------------------------------------------------------------------
# 标题中出现这些词的，是独立学院 / 考研升学内容，与本科转专业无关
_INDEPENDENT_COLLEGE = ("城市学院", "宁波理工", "宁波大学", "海宁国际",
                        "伊利诺伊", "国际联合学院", "舟山")
_GRADUATE_WORDS = ("考研", "考博", "专硕", "学硕", "347", "312", "研究生",
                   "硕士", "博士", "复试", "调剂")


def relevance(video: dict):
    """返回命中等级：1=心理/脑机直接相关，2=浙大转专业通用（限额），0=不相关。

    B 站简介常被塞关键词（考研视频简介里写"转专业"），所以一律以
    【标题】判断，不看简介，避免误收。
    """
    title = re.sub(r"<.*?>", "", video.get("title", ""))
    if any(w in title for w in _INDEPENDENT_COLLEGE):
        return 0
    is_graduate = any(w in title for w in _GRADUATE_WORDS)
    has_zju = ("浙大" in title) or ("浙江大学" in title)
    has_psych = ("心理" in title) or ("脑机" in title)
    has_transfer = any(w in title for w in
                       ("转专业", "专业确认", "大类分流", "主修专业"))
    # 规则1：标题直接讲转专业，且与心理/脑机 或 浙大相关
    if has_transfer and not is_graduate and (has_psych or has_zju):
        return 1 if has_psych else 2
    return 0


def video_to_item(video: dict, subtitle: str) -> dict:
    """把搜索结果 + 字幕拼成知识库文章结构。"""
    title = re.sub(r"<.*?>", "", video.get("title", "")).strip()
    ts = video.get("pubdate")
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
    # 注意：这里只放稳定字段。播放量/收藏数/弹幕数是实时变化的，
    # 若写进正文会导致 content_hash 每天不同、每天误报"更新"。
    parts = [
        f"UP主：{video.get('author', '')}",
        f"发布日期：{date or '（未知）'}",
        f"简介：{video.get('description', '') or '（无简介）'}",
    ]
    if subtitle:
        parts.append("字幕（自动转写，可能有错字）：\n" + subtitle)
    return {
        "source_code": "bilibili",
        "title": title,
        "date": date,
        "href": "https://www.bilibili.com/video/" + video.get("bvid", ""),
        "summary": "\n".join(parts),
        "source": "B站",
        "category": "经验帖",
        "attachments": [],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def collect(dry_run: bool = False, with_subtitle: bool = True, log=print) -> dict:
    """搜索 → 过滤 → 字幕 → 入库。"""
    cli = BiliClient()
    log("① 初始化 B 站会话与 wbi 签名……")
    cli.prepare()

    # bvid -> (video, 等级) 去重
    picked = {}
    general_count = 0
    for kw in SEARCH_KEYWORDS:
        log(f"② 搜索：{kw}")
        try:
            rows = cli.search(kw)
        except Exception as e:
            log(f"  搜索失败（跳过）：{type(e).__name__}: {str(e)[:80]}")
            time.sleep(SEARCH_SLEEP)
            continue
        log(f"  原始命中 {len(rows)} 条")
        for v in rows:
            bvid = v.get("bvid")
            if not bvid or bvid in picked:
                continue
            level = relevance(v)
            if level == 0:
                continue
            if level == 2:
                if general_count >= MAX_GENERAL_ZJU:
                    continue
                general_count += 1
            picked[bvid] = (v, level)
        time.sleep(SEARCH_SLEEP)

    log(f"③ 过滤后保留 {len(picked)} 个视频")
    stat = {"saved": len(picked), "new": 0, "updated": 0, "unchanged": 0}

    con = kb.get_db()
    view_failures = 0
    try:
        for bvid, (v, level) in picked.items():
            if view_failures >= MAX_VIEW_FAILURES:
                log("  详情接口连续失败，疑似处于风控窗口，剩余视频明天再收")
                break
            # 入库数据必须来自详情接口（稳定）。详情拿不到就跳过本轮：
            # 搜索接口的简介会随搜索词抖动，用它兜底会污染已入库内容，
            # 宁可让新视频晚一天入库，也不写脏数据。
            try:
                view = cli.get_view(bvid)
                view_failures = 0
            except Exception as e:
                view_failures += 1
                log(f"  详情获取失败，跳过该视频：{str(e)[:80]}")
                time.sleep(VIEW_412_SLEEP)
                continue
            stable = dict(v)
            stable["title"] = view.get("title") or v.get("title")
            stable["description"] = view.get("desc", "") or ""
            stable["author"] = (view.get("owner") or {}).get("name",
                                                              v.get("author"))
            stable["pubdate"] = view.get("pubdate", v.get("pubdate"))
            time.sleep(REQUEST_SLEEP)

            item = video_to_item(stable, "")
            tag = "强相关" if level == 1 else "浙大通用"
            if dry_run:
                log(f"  [DRY][{tag}] {item['date']} {item['title'][:40]}")
                continue
            # 先入库（无字幕）；只有"新入库"的视频才去抓字幕，
            # 老视频不再重复请求，每日运行又快又稳
            st = kb.upsert_article(con, item)
            if st == "new" and with_subtitle:
                try:
                    pages = cli.get_pages(bvid)
                    if pages:
                        urls = cli.get_subtitle_urls(bvid, pages[0]["cid"])
                        if urls:
                            subtitle = cli.get_subtitle_text(urls[0])[
                                :MAX_SUBTITLE_CHARS]
                            if subtitle:
                                kb.upsert_article(con,
                                                  video_to_item(stable, subtitle))
                    time.sleep(REQUEST_SLEEP)
                except Exception as e:
                    log(f"  字幕获取失败（仅存简介）：{str(e)[:80]}")
            stat[st] += 1
            mark = {"new": "新入库", "updated": "有更新",
                    "unchanged": "无变化"}[st]
            log(f"  [{tag}][{mark}] {item['title'][:40]}")
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="B 站转专业经验视频采集")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--no-subtitle", action="store_true", help="不抓取字幕")
    args = ap.parse_args()
    try:
        st = collect(dry_run=args.dry_run,
                     with_subtitle=not args.no_subtitle)
    except Exception as e:
        print(f"采集失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("-" * 50)
    if args.dry_run:
        print(f"试运行完成：通过过滤 {st['saved']} 个视频（未写库）")
    else:
        print(f"完成：新入库 {st['new']}，更新 {st['updated']}，"
              f"无变化 {st['unchanged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
