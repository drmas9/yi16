# -*- coding: utf-8 -*-
"""
CC98 转专业经验帖采集（P3 第一步：非官方经验）
==============================================================
用官方只读 REST API 搜索 CC98 论坛里与「心理系 / 脑机智能创新班转专业」
相关的历史讨论帖，把整帖（楼主正文 + 全部回复楼层）下载后写入
「心理系知识库」的同一张文章表，来源标记为 cc98、分类为「经验帖」。
入库后可以直接被知识库全文检索搜到，也会出现在每日必读提醒里。

为什么不用大词"转专业"直接搜：
    · 该词在 CC98 极高频，前几页全是其他专业，噪声大；
    · CC98 搜索接口有频率限制，连续搜索会返回 403。
    所以默认只搜少量精确词；确实要慢扫大词时加 --deep。

用法：
    python -B CC98经验帖采集.py              # 精确词搜索 + 抓楼层 + 入库
    python -B CC98经验帖采集.py --dry-run    # 只打印命中，不写库
    python -B CC98经验帖采集.py --deep       # 额外慢扫"转专业/心理系"大词
    python -B CC98经验帖采集.py --no-floors  # 不抓楼层，只取搜索摘要（快）

全部为只读操作，不会发帖、回帖或点赞。
"""

import argparse
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

# 复用同目录下已有的 CC98 客户端（含登录鉴权）、UBB 清洗和常量
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cc98爬虫 as cc98                      # noqa: E402
import 心理系知识库 as kb                    # noqa: E402

# ---------------------------------------------------------------------------
# 搜索策略
# ---------------------------------------------------------------------------
# 默认精确词：命中即高度相关，请求量小
SEARCH_KEYWORDS = [
    "心理系转专业",
    "脑机智能创新班",
    "脑机智能 转专业",
]
# --deep 时额外慢扫的大词：结果需要客户端再过滤
DEEP_KEYWORDS = ["转专业", "心理系"]

SEARCH_SIZE = 20          # 每次搜索取回多少条
SEARCH_SLEEP = 3.0        # 两次搜索之间的礼貌间隔（秒）
FLOOR_SLEEP = 1.5         # 两次抓楼层之间的礼貌间隔
FLOOR_PAGE_SIZE = 20      # 楼层接口每页条数
MAX_FLOOR_PAGES = 3       # 每帖最多抓几页楼层（即最多 60 楼）
MAX_CHARS_PER_FLOOR = 5000   # 单个楼层最多保留多少字
MAX_CHARS_PER_TOPIC = 100_000  # 整帖入库文本上限

# 403 限速退避秒数（第 1 次重试等 20 秒，第 2 次等 40 秒）
BACKOFF_403 = [20, 40]


# ---------------------------------------------------------------------------
# 带限速退避的请求
# ---------------------------------------------------------------------------
def api_with_backoff(client: cc98.Cc98Client, path: str, tries: int = 3):
    """调用 CC98 API；遇到 403 限速时按 BACKOFF_403 等待后重试。"""
    last_err = None
    for i in range(tries):
        if i > 0:
            wait = BACKOFF_403[min(i - 1, len(BACKOFF_403) - 1)]
            print(f"      （触发限速，等待 {wait} 秒后重试……）")
            time.sleep(wait)
        try:
            return client.api(path)
        except RuntimeError as e:
            last_err = e
            if "403" not in str(e) or i == tries - 1:
                raise
    raise last_err


# ---------------------------------------------------------------------------
# 相关性判断
# ---------------------------------------------------------------------------
def is_relevant(title: str, body: str) -> bool:
    """判断帖子是否与"心理系/脑机智能创新班转专业"相关。

    搜索接口的大词结果很杂（课程吐槽、被试招募、高考咨询等），
    入库前用关键词组合再过滤一道：
      规则1：文本同时出现"心理"和"转专业/转入"；
      规则2：文本同时出现"脑机"和"创新班/转专业/选拔"等。
    body 只检查前 3000 字（搜索摘要 + 首楼开头通常足够判断）。
    """
    text = f"{title}\n{body[:3000]}"
    transfer_words = ("转专业", "转入", "转出")
    if "心理" in text and any(w in text for w in transfer_words):
        return True
    if "脑机" in text and any(w in text for w in
                              ("创新班", "转专业", "转入", "选拔", "招生")):
        return True
    return False


# ---------------------------------------------------------------------------
# 搜索 + 楼层下载
# ---------------------------------------------------------------------------
def search_topics(client, keyword: str) -> list:
    """按一个关键词搜索，返回话题原始 JSON 列表。"""
    q = urllib.parse.quote(keyword)
    rows = api_with_backoff(
        client, f"/topic/search?keyword={q}&from=0&size={SEARCH_SIZE}"
    )
    return rows or []


def fetch_posts(client, topic_id: int, reply_count: int) -> list:
    """翻页下载一个帖子的楼层（首楼 + 回复），按楼层顺序返回。"""
    pages = min(MAX_FLOOR_PAGES,
                max(1, (reply_count + FLOOR_PAGE_SIZE) // FLOOR_PAGE_SIZE))
    posts = []
    for p in range(pages):
        frm = p * FLOOR_PAGE_SIZE
        batch = api_with_backoff(
            client,
            f"/topic/{topic_id}/post?from={frm}&size={FLOOR_PAGE_SIZE}",
        )
        if not batch:
            break
        posts.extend(batch)
        if len(batch) < FLOOR_PAGE_SIZE:
            break
        time.sleep(FLOOR_SLEEP)
    # 按楼层号排序，防止接口乱序
    posts.sort(key=lambda x: x.get("floor", 0))
    return posts


def topic_to_item(topic: dict, posts: list) -> dict:
    """把 CC98 话题 + 楼层拼成知识库文章结构（与官网爬虫的 item 同构）。"""
    lines = []
    total = 0
    for i, p in enumerate(posts):
        content = cc98.clean_ubb(p.get("content", ""))[:MAX_CHARS_PER_FLOOR]
        if not content:
            continue
        user = p.get("userName") or ("匿名用户" if p.get("isAnonymous") else "网友")
        ptime = (p.get("time") or "")[:10]
        if i == 0:
            head = f"【楼主】{user}"
        else:
            head = f"【{p.get('floor', i + 1)}楼】{user}"
        block = f"{head}（{ptime}）：{content}"
        total += len(block)
        if total > MAX_CHARS_PER_TOPIC:
            lines.append("……（楼层过多，后续回复已截断）")
            break
        lines.append(block)

    # 没抓到楼层时，退回搜索结果自带的末楼摘要
    if not lines:
        fallback = cc98.clean_ubb(topic.get("lastPostContent", ""))
        lines = [f"（未取到楼层，末楼摘要）{fallback}"]

    dt = cc98.parse_time(topic.get("time"))
    return {
        "source_code": "cc98",
        "title": topic.get("title", "").strip(),
        "date": dt.strftime("%Y-%m-%d") if dt else "",
        "href": cc98.FORUM_BASE + str(topic["id"]),
        "summary": "\n\n".join(lines),
        "source": f"CC98·{topic.get('boardName', '')}",
        "category": "经验帖",
        "attachments": [],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def collect(deep: bool = False, with_floors: bool = True,
            dry_run: bool = False, log=print) -> dict:
    """搜索 → 过滤 → 抓楼层 → 入库。

    返回统计字典：searched / candidates / saved / new / updated / unchanged。
    """
    client = cc98.Cc98Client(cc98.CC98_USERNAME, cc98.CC98_PASSWORD)
    client.login()
    log("CC98 登录成功")

    keywords = list(SEARCH_KEYWORDS)
    if deep:
        keywords += DEEP_KEYWORDS

    # 按帖子 id 去重，命中多个关键词的帖子只处理一次
    topics = {}
    for kw in keywords:
        log(f"① 搜索关键词：{kw}")
        try:
            rows = search_topics(client, kw)
        except Exception as e:
            log(f"  搜索失败（跳过）：{type(e).__name__}: {str(e)[:100]}")
            time.sleep(SEARCH_SLEEP)
            continue
        log(f"  原始命中 {len(rows)} 条")
        for t in rows:
            tid = t.get("id")
            if tid and tid not in topics:
                topics[tid] = t
        time.sleep(SEARCH_SLEEP)

    log(f"② 合并去重后候选 {len(topics)} 帖，开始相关性过滤……")
    picked = []
    for t in topics.values():
        title = t.get("title", "")
        brief = cc98.clean_ubb(t.get("lastPostContent", ""))
        # 精确词来源直接收；大词结果走规则过滤
        if is_relevant(title, brief):
            picked.append(t)
    log(f"③ 过滤后保留 {len(picked)} 帖")

    stat = {"searched": len(keywords), "candidates": len(topics),
            "saved": len(picked), "new": 0, "updated": 0, "unchanged": 0}

    con = kb.get_db()
    try:
        for t in picked:
            tid = t["id"]
            posts = []
            if with_floors:
                log(f"④ 抓楼层：{t.get('title', '')[:40]}")
                try:
                    posts = fetch_posts(client, tid, t.get("replyCount", 0))
                except Exception as e:
                    log(f"  楼层抓取失败（用摘要兜底）：{str(e)[:80]}")
            item = topic_to_item(t, posts)
            if dry_run:
                tag = f"{len(posts)}楼" if posts else "仅摘要"
                log(f"  [DRY] {item['date']} [{item['source']}] "
                    f"{item['title']}（{tag}）")
                continue
            st = kb.upsert_article(con, item)
            stat[st] += 1
            mark = {"new": "新入库", "updated": "有更新",
                    "unchanged": "无变化"}[st]
            log(f"  {mark}：{item['title'][:40]}")
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CC98 转专业经验帖采集")
    ap.add_argument("--deep", action="store_true",
                    help="额外慢扫'转专业/心理系'大词（更慢，请求更多）")
    ap.add_argument("--no-floors", action="store_true",
                    help="不下载楼层，只取搜索摘要")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印会收录哪些帖子，不写入知识库")
    args = ap.parse_args()

    try:
        st = collect(deep=args.deep, with_floors=not args.no_floors,
                     dry_run=args.dry_run)
    except Exception as e:
        print(f"采集失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("-" * 50)
    if args.dry_run:
        print(f"试运行完成：搜索 {st['searched']} 个词，候选 {st['candidates']} 帖，"
              f"通过过滤 {st['saved']} 帖（未写库）")
    else:
        print(f"完成：新入库 {st['new']}，更新 {st['updated']}，"
              f"无变化 {st['unchanged']}（通过过滤共 {st['saved']} 帖）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
