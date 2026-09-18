# -*- coding: utf-8 -*-
"""
浙大心理与行为科学系 · 本地情报知识库（P1）
==========================================

这个模块在「心理系爬虫.py」之上增加一层"知识库"，解决三个问题：

1. 信息多、文件大 —— 抓回来的通知全文与 PDF/Word/Excel 附件统一解析成文本，
   存进一个 SQLite 数据库文件（psych_knowledge.db），并建立全文检索索引，
   以后按关键词秒查，不用反复翻网页。
2. 转专业关键信息分散 —— 用规则从转专业通知中自动抽取结构化要素：
   报名时间 / 绩点门槛 / 名额 / 考核方式 / 面试时间地点 / 材料清单 /
   公示时间 / 咨询方式，方便把不同学期的通知放在一起对比。
3. 重复与更新 —— 同一链接以内容指纹判断是否变化；同主题（不同学年的同类
   通知）用归一化标题做"主题键"，便于追溯历年版本。

数据库里有 4 张表 + 1 个全文索引：
    articles      通知文章（标题/日期/栏目/分类/正文/指纹/主题键/是否已读）
    attachments   附件（文件名/链接/类型/解析出的文本/状态）
    key_facts     从文章中抽取出的关键要素（类型/值/原句上下文）
    articles_fts  FTS5 全文索引（trigram 分词，对中文友好）

附件解析依赖（均为可选，缺失时自动降级为"只登记不解析"）：
    pypdf        解析 PDF
    python-docx  解析 .docx
    openpyxl     解析 .xlsx
    旧版 .doc/.xls 无法在不装 Office 的情况下解析，标记为 unsupported。

用法示例：
    python 心理系知识库.py sync                    # 抓取 + 入库（转专业类附件自动下载解析）
    python 心理系知识库.py sync --all-attachments  # 所有附件都下载解析
    python 心理系知识库.py search 绩点             # 全文检索
    python 心理系知识库.py facts                   # 查看转专业关键要素对比
    python 心理系知识库.py stats                   # 库存统计
    python 心理系知识库.py recent --category 转专业
"""

import argparse
import hashlib
import io
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 复用同目录爬虫模块的请求头、抓取函数与常量
sys.path.insert(0, str(Path(__file__).resolve().parent))
import 心理系爬虫 as psych_crawler  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DB_FILE = Path(__file__).resolve().parent / "psych_knowledge.db"

SOURCE_CODE = "psych-official"      # 文章来源代号（以后接入 CC98 等新来源时区分用）

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024   # 单个附件下载大小上限：15 MB
MAX_TEXT_PER_ATTACH = 200_000             # 单个附件入库文本上限：约 20 万字符
ATTACH_SLEEP = 0.4                        # 每下载一个附件后的礼貌间隔（秒）

# 要素抽取时，每篇文章同一类型最多保留多少条，避免规则误报造成噪声
MAX_FACTS_PER_TYPE = 5


# ===========================================================================
# 一、数据库初始化
# ===========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY,
    source_code  TEXT NOT NULL,
    href         TEXT NOT NULL UNIQUE,      -- 原文链接，天然唯一，用作去重键
    title        TEXT NOT NULL,
    date         TEXT,                       -- 发布日期 YYYY-MM-DD
    column_name  TEXT,                       -- 栏目名
    category     TEXT,                       -- 爬虫给出的分类（转专业/推免/……）
    content      TEXT,                       -- 正文纯文本
    content_hash TEXT,                       -- 标题+正文的指纹，用于识别"内容是否变了"
    topic_key    TEXT,                       -- 归一化主题键：同系列通知（不同学年）归为一族
    is_read      INTEGER NOT NULL DEFAULT 0, -- 是否已读（P2 每日 18:00 必读确认会用到）
    first_seen   TEXT,                       -- 首次入库时间
    last_updated TEXT                        -- 最近一次内容变化时间
);

CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    filename   TEXT,
    url        TEXT UNIQUE,                  -- 附件链接唯一
    ext        TEXT,                         -- pdf / docx / xlsx / doc / xls
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending/ok/unsupported/failed/too_large
    text       TEXT,                         -- 解析出的文本（解析失败时为空）
    size       INTEGER,                      -- 文件字节数
    error      TEXT,                         -- 失败原因（成功时为空）
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS key_facts (
    id         INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    fact_type  TEXT,                         -- 要素类型（报名时间/绩点门槛/……）
    fact_value TEXT,                         -- 抽取到的关键值
    context    TEXT,                         -- 原句上下文，方便人工核对规则结果
    UNIQUE(article_id, fact_type, fact_value)
);

-- FTS5 全文索引：trigram 三元组分词，中文按任意 3 字片段即可命中
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    article_id UNINDEXED,
    title,
    content,
    attach_text,
    tokenize = 'trigram'
);

CREATE INDEX IF NOT EXISTS idx_articles_date     ON articles(date);
CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);
CREATE INDEX IF NOT EXISTS idx_articles_topic    ON articles(topic_key);
CREATE INDEX IF NOT EXISTS idx_facts_article     ON key_facts(article_id);
CREATE INDEX IF NOT EXISTS idx_att_article       ON attachments(article_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_db(path: Path = DB_FILE) -> sqlite3.Connection:
    """打开（不存在则创建）知识库并初始化表结构。"""
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")
    con.commit()
    return con


# ===========================================================================
# 二、标题归一化（同主题归并）
# ===========================================================================
def topic_key(title: str) -> str:
    """把标题去掉年份/学期/日期/更新标记，生成"主题键"。

    例：
      「心理系2026-2027秋冬学期转专业面试通知」
      「心理系2025-2026春夏学期转专业面试通知」
      归一化后主题键相同，可以视为同一事项的不同学年版本。
    """
    t = title
    t = re.sub(r"20\d{2}\s*[-—–~～]\s*20\d{2}\s*学年?", " ", t)  # 2026-2027学年
    t = re.sub(r"20\d{2}\s*年", " ", t)                         # 2026年
    t = re.sub(r"(秋冬|春夏|春季|秋季|冬季|夏季)\s*学期", " ", t)
    t = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*日?", " ", t)
    t = re.sub(r"[（(][^）)]*更新[^）)]*[）)]", " ", t)          # （9月11日更新）
    t = re.sub(r"[\s【】\[\]（）()《》<>，,。.；;：:、\-—–_/\\]+", "", t)
    return t.strip().lower()


# ===========================================================================
# 三、转专业关键要素抽取（规则法）
# ===========================================================================
# 钟点时间，如 14:00、17:30、14:00-15:00
_CLOCK = (r"(?:\s*\d{1,2}\s*[:：]\s*\d{2}"
          r"(?:\s*[-—~～至到]\s*\d{1,2}\s*[:：]\s*\d{2})?)?")
# 日期区间的后半段，如 "至9月11日16:00"、"-9月24日"
_RANGE = (r"(?:\s*(?:至|到|[-—~～])\s*"
          r"(?:20\d{2}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*日?" + _CLOCK + r")?")
# 完整日期/时间段表达式（只认中文"年月日"写法，避免把编号"1.7"误判成日期）：
#   9月18日、2026年9月18日、9月18日（周五）、9月18日14:00-15:00、
#   7月6日9:00至9月11日16:00、9月19日-9月24日
DATE_TIME_RE = re.compile(
    r"(?:20\d{2}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*日?"
    r"(?:\s*[（(][^）)]{0,12}[）)])?"
    + _CLOCK + _RANGE
)


def split_sentences(text: str) -> list:
    """把正文切成有意义的片段。

    官网通知的 meta 全文常常没有句号，整篇连成一大段，但编号列表
    （"一、""1."）本身就是天然分隔，所以先在这些编号前插入换行，
    再按句号/分号/换行切分。
    """
    t = text or ""
    # 先剔除网址：它不含日期信息，却会撑大锚点与日期之间的距离
    t = re.sub(r"https?://[^\s，。；;）)】」』]+", " ", t)
    # "一、二、……" 形式的中文编号前断行
    t = re.sub(r"(?=[一二三四五六七八九十]+、)", "\n", t)
    # "1. 2. ……" 形式的阿拉伯数字编号前断行。
    # 编号点后可能直接跟数字（如"1.7月6日"），无法与小数严格区分，
    # 但限制编号为 1~2 位、其前不是数字/字母，误切对后续锚点匹配无害。
    t = re.sub(r"(?<![\dA-Za-z.])(?=\d{1,2}[.、])", "\n", t)
    parts = re.split(r"(?<=[。；;！!？?])|[\r\n]+", t)
    return [p.strip(" 　\t") for p in parts if p and p.strip()]


def _times_by_anchor(text: str) -> dict:
    """把片段中的每个日期归给离它"最近"的时间锚点。

    返回 {要素类型: [日期, ...]}，如 {"面试时间": ["9月18日14:00-15:00"]}。
    同一段里可能同时出现"面试时间：……院系公示时间：……"两个标签和两个
    日期，若只按"在窗口内"判断，面试日期也会落入公示锚点的窗口；因此
    对每个日期比较它到所有锚点的间隔，只归给最近的那个锚点类型，
    并且仍受该类型自己的窗口上限约束。
    """
    all_dates = [(m.group(0), m.span()) for m in DATE_TIME_RE.finditer(text)]
    if not all_dates:
        return {}
    anchors = []  # (起点, 终点, 要素类型, 窗口)
    for fact_type, (pat, win) in ANCHORS.items():
        for m in pat.finditer(text):
            anchors.append((m.start(), m.end(), fact_type, win))

    result = {}
    for raw, (d_start, d_end) in all_dates:
        best = None  # (间隔, 要素类型)
        for a_start, a_end, fact_type, win in anchors:
            if d_end <= a_start:
                gap = a_start - d_end        # 日期在锚点前面
            elif d_start >= a_end:
                gap = d_start - a_end        # 日期在锚点后面
            else:
                gap = -1                     # 相互重叠（中文锚点实际不会发生）
            if gap <= win and (best is None or gap < best[0]):
                best = (gap, fact_type)
        if best is None:
            continue
        value = re.sub(r"\s+", "", raw)
        if not re.search(r"\d{1,2}月\d{1,2}", value):
            continue
        bucket = result.setdefault(best[1], [])
        if value not in bucket:
            bucket.append(value)
    return result


# 各类时间要素对应的锚点词与就近窗口（字符数）。
# 报名类通知常见写法是"X月X日至X月X日，学生可在……平台提交申请"，
# 日期与锚点之间隔着平台名称，所以报名窗口给大一些；
# 面试/公示条目紧凑，窗口小一点以避免互相串味。
ANCHORS = {
    "报名时间": (re.compile(r"报名|提交[^，。；]{0,10}申请|填报|系统\s*(?:开放|关闭)"), 90),
    # "面试地点/面试形式/面试安排"等也含"面试"二字，但与时间无关，用负向预查排除
    "面试时间": (re.compile(r"面试时间|面试(?!地点|形式|专家|顺序|安排|流程|名单|考核|组)"), 30),
    "公示时间": (re.compile(r"公示时间|公示期|公示"), 30),
}


def extract_facts(content: str, category: str) -> list:
    """从通知正文中抽取关键要素。

    返回三元组列表：(要素类型, 关键值, 原句上下文)。
    规则法可能有误报，所以每条都保留原句，方便人工核对。
    """
    facts = []
    seen = set()       # (类型, 值) 去重

    def add(fact_type, value, ctx):
        value = re.sub(r"\s+", " ", value).strip()
        ctx = re.sub(r"\s+", " ", ctx).strip()
        if not value or len(value) > 120:
            return
        key = (fact_type, value)
        if key in seen:
            return
        # 每类要素数量封顶
        if sum(1 for f in facts if f[0] == fact_type) >= MAX_FACTS_PER_TYPE:
            return
        seen.add(key)
        facts.append((fact_type, value, ctx[:200]))

    segments = split_sentences(content)
    exam_words = set()     # 考核方式跨片段汇总，最后只输出一条
    for s in segments:
        # ---- 时间类：每个日期归给最近的锚点（报名/面试/公示） ----
        for fact_type, dates_ in _times_by_anchor(s).items():
            for d in dates_:
                add(fact_type, d, s)

        # ---- 面试地点：抓"面试地点："后面的内容，到空白/标点/下一编号为止 ----
        m = re.search(r"面试地点\s*[：:]\s*([^\s，。；;（）()二三四五六七八九十]{4,40})", s)
        if m:
            add("面试地点", m.group(1).strip(), s)

        # ---- 绩点门槛：必须带门槛语义（不低于/达到/以上等），
        #      避免把成绩公示表格里的"平均绩点 4.92"误当成门槛 ----
        for gm in re.finditer(
            r"(?:平均(?:学分)?)?绩点[^。\n]{0,12}?"
            r"(?:不低于|不小于|达到|要求|应为|需要|需在|须在|≥|>=)"
            r"[^。\n]{0,6}?(\d\.\d{1,2})"
            r"|(?:平均(?:学分)?)?绩点[^。\n]{0,6}?(\d\.\d{1,2})\s*(?:及以上|以上|含以上|含)"
            r"|(\d\.\d{1,2})\s*(?:及以上|以上)[^。\n]{0,10}?绩点", s
        ):
            num = gm.group(1) or gm.group(2) or gm.group(3)
            add("绩点门槛", num, s)

        # ---- 名额：句子要先有"录取/招收/名额/计划"语境，再抓"N名" ----
        if re.search(r"名额|招收|招录|拟?录取|计划", s):
            for qm in re.finditer(r"(\d{1,3})\s*名(?!额)", s):
                add("招生名额", qm.group(1) + "名", s)

        # ---- 考核方式：跨片段汇总命中词，最后合并成一条输出 ----
        if re.search(r"考核|面试|笔试|录取|遴选", s):
            for w in ("笔试", "面试", "综合考评", "多对一", "机考", "实操"):
                if w in s:
                    exam_words.add(w)

        # ---- 申请条件：出现"条件/资格"说明的片段 ----
        if re.search(r"(申请|报名)\s*(条件|资格)|符合[^，。；]{0,12}条件|以下条件", s):
            add("申请条件", s, s)

        # ---- 材料清单：片段提到"材料"且含提交/表单/证明等线索 ----
        if "材料" in s and re.search(r"提交|准备|以下|如下|申请表|证明|成绩单|复印件|扫描件", s):
            add("材料清单", s, s)

    if exam_words:
        # 按固定表述顺序输出，如"面试、多对一、综合考评"
        order = ("笔试", "面试", "综合考评", "多对一", "机考", "实操")
        add("考核方式", "、".join(w for w in order if w in exam_words), "")

    # ---- 联系方式：电话/邮箱必须带数字或单词边界，避免把名单表格的学号误判为电话 ----
    for pm in re.finditer(r"(?<!\d)0\d{2,3}\s*[-—]?\s*\d{7,8}(?!\d)", content):
        add("咨询电话", re.sub(r"\s+", "", pm.group(0)), "")
    for em in re.finditer(r"(?<![\w.])[A-Za-z0-9_.\-]+@[A-Za-z0-9_.\-]+\.[A-Za-z]{2,}(?![\w.])",
                          content):
        add("咨询邮箱", em.group(0), "")

    return facts


# ===========================================================================
# 四、附件下载与解析（可选依赖，失败降级）
# ===========================================================================
class AttachmentError(Exception):
    """附件下载/解析过程中的预期内错误（状态记为 failed/unsupported）。"""


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader                 # 第三方包，按需导入
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _parse_docx(data: bytes) -> str:
    import docx                                 # python-docx
    doc = docx.Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    # 表格内容：每行单元格用 " | " 拼接
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _parse_xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"# 工作表：{ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None]
            if any(cells):
                lines.append(" | ".join(cells))
    wb.close()
    return "\n".join(lines)


# 扩展名 -> 解析函数（旧版 doc/xls 不在表中，标记 unsupported）
PARSERS = {"pdf": _parse_pdf, "docx": _parse_docx, "xlsx": _parse_xlsx}


def download_attachment(url: str) -> bytes:
    """下载附件的二进制内容，超过大小上限时抛错。"""
    req = urllib.request.Request(url, headers=psych_crawler.HEADERS)
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = resp.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(f"超过 {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB 大小上限")
    return data


def parse_attachment_bytes(data: bytes, ext: str) -> str:
    """按扩展名解析附件内容为纯文本。"""
    parser = PARSERS.get(ext)
    if parser is None:
        raise AttachmentError("旧版 .doc/.xls 格式，请在官网另存为 PDF/docx/xlsx 后再解析")
    return _clean_unicode(parser(data))


def _clean_unicode(text: str) -> str:
    """清洗解析文本。

    PDF 提取数学公式符号时可能留下孤立的 UTF-16 代理字符
    （如 𝐵 的半个码位 \\ud835），它无法编码成 UTF-8，会导致
    写入 SQLite 时报 UnicodeEncodeError，这里直接剔除。
    """
    if not text:
        return ""
    return re.sub(r"[\ud800-\udfff]", "", text)


# ===========================================================================
# 五、入库（文章 / 要素 / 附件 / 全文索引）
# ===========================================================================
def _hash(title: str, content: str) -> str:
    """标题+正文的 SHA1 指纹。"""
    return hashlib.sha1((title + "\x00" + content).encode("utf-8", "ignore")).hexdigest()


def _refresh_fts(con: sqlite3.Connection, article_id: int) -> None:
    """根据 articles/attachments 表的最新内容，重建某篇文章的全文索引行。"""
    con.execute("DELETE FROM articles_fts WHERE article_id = ?", (str(article_id),))
    row = con.execute(
        "SELECT title, content FROM articles WHERE id = ?", (article_id,)
    ).fetchone()
    attach_rows = con.execute(
        "SELECT text FROM attachments WHERE article_id = ? AND status = 'ok'",
        (article_id,),
    ).fetchall()
    attach_text = "\n".join(r["text"] or "" for r in attach_rows)
    con.execute(
        "INSERT INTO articles_fts(article_id, title, content, attach_text) "
        "VALUES(?, ?, ?, ?)",
        (str(article_id), row["title"] or "", row["content"] or "", attach_text),
    )


def upsert_article(con: sqlite3.Connection, item: dict) -> str:
    """把一条爬取结果写入 articles 表。

    返回三种状态：
        new        新链接，首次入库
        updated    链接已存在，但正文指纹变了（更新并重新抽取要素）
        unchanged  链接和内容都没变，跳过
    """
    title = item.get("title", "").strip()
    content = item.get("summary", "") or ""
    href = item["href"]
    # 来源可由调用方覆盖（如 CC98 经验帖采集器传 "cc98"），默认官网
    source_code = item.get("source_code", SOURCE_CODE)
    now = datetime.now().isoformat(timespec="seconds")
    h = _hash(title, content)

    row = con.execute("SELECT id, content_hash FROM articles WHERE href = ?", (href,)).fetchone()
    if row is not None and row["content_hash"] == h:
        return "unchanged"

    if row is None:
        cur = con.execute(
            "INSERT INTO articles(source_code, href, title, date, column_name, category,"
            " content, content_hash, topic_key, is_read, first_seen, last_updated)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (source_code, href, title, item.get("date", ""), item.get("source", ""),
             item.get("category", "其他"), content, h, topic_key(title), now, now),
        )
        article_id = cur.lastrowid
        status = "new"
    else:
        article_id = row["id"]
        con.execute(
            "UPDATE articles SET source_code=?, title=?, date=?, column_name=?, category=?, content=?,"
            " content_hash=?, topic_key=?, last_updated=? WHERE id=?",
            (source_code, title, item.get("date", ""), item.get("source", ""),
             item.get("category", "其他"), content, h, topic_key(title), now, article_id),
        )
        # 正文变了，旧要素作废重抽
        con.execute("DELETE FROM key_facts WHERE article_id = ?", (article_id,))
        status = "updated"

    # 关键要素抽取只对官网通知做；论坛经验帖是口语化讨论，规则抽取会产生噪声
    if source_code == SOURCE_CODE:
        facts = extract_facts(content, item.get("category", ""))
    else:
        facts = []
    for fact_type, value, ctx in facts:
        con.execute(
            "INSERT OR IGNORE INTO key_facts(article_id, fact_type, fact_value, context)"
            " VALUES(?, ?, ?, ?)",
            (article_id, fact_type, value, ctx),
        )

    _refresh_fts(con, article_id)
    return status


def sync_attachments(con: sqlite3.Connection, article_id: int, metas: list,
                     download_enabled: bool, log=print) -> int:
    """同步一篇文章的附件：登记元数据，按策略下载并解析文本。

    metas：爬虫解析出的附件列表 [{filename, url, ext}, ...]
    download_enabled=False 时只登记（status=pending），不下载。
    返回本次成功解析（status=ok）的附件数量。
    """
    ok_count = 0
    for meta in metas:
        existing = con.execute(
            "SELECT id, status FROM attachments WHERE url = ?", (meta["url"],)
        ).fetchone()
        if existing is None:
            # 先登记附件元数据，即使本次不下载，库里也看得到"有哪些附件"
            con.execute(
                "INSERT INTO attachments(article_id, filename, url, ext, status)"
                " VALUES(?, ?, ?, ?, 'pending')",
                (article_id, meta["filename"], meta["url"], meta["ext"]),
            )
            att_id = con.execute(
                "SELECT id FROM attachments WHERE url = ?", (meta["url"],)).fetchone()["id"]
        else:
            att_id = existing["id"]
            # 已经解析成功的附件不重复下载；失败的允许在 enabled 时重试
            if existing["status"] == "ok" or not download_enabled:
                if existing["status"] == "ok":
                    ok_count += 1
                continue

        if not download_enabled:
            continue

        now = datetime.now().isoformat(timespec="seconds")
        try:
            log(f"      ↓ 下载附件：{meta['filename'][:50]}")
            data = download_attachment(meta["url"])
            time.sleep(ATTACH_SLEEP)
            text = parse_attachment_bytes(data, meta["ext"])
            con.execute(
                "UPDATE attachments SET status='ok', text=?, size=?, error='', fetched_at=?"
                " WHERE id=?",
                (text[:MAX_TEXT_PER_ATTACH], len(data), now, att_id),
            )
            ok_count += 1
        except AttachmentError as e:
            # 业务预期内的失败（旧格式、超大）
            con.execute(
                "UPDATE attachments SET status='unsupported', error=?, fetched_at=?"
                " WHERE id=?", (str(e), now, att_id))
        except ImportError as e:
            # 缺少解析库：标记失败并给出安装提示，不影响其他附件
            con.execute(
                "UPDATE attachments SET status='failed', error=?, fetched_at=?"
                " WHERE id=?", (f"缺少解析依赖：{e.name}（可 pip install）", now, att_id))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            con.execute(
                "UPDATE attachments SET status='failed', error=?, fetched_at=?"
                " WHERE id=?", (f"下载失败：{e}", now, att_id))
        except Exception as e:  # 兜底：任何意外（编码、解析器内部错误等）只标记，不拖垮整轮同步
            con.execute(
                "UPDATE attachments SET status='failed', error=?, fetched_at=?"
                " WHERE id=?", (f"解析失败：{type(e).__name__}: {e}", now, att_id))

    # 附件文本可能变化，重建该文索引
    con.execute("SELECT 1 FROM attachments WHERE article_id=? AND status='ok'",
                (article_id,)).fetchone()
    _refresh_fts(con, article_id)
    return ok_count


def sync(pages: int = 1, all_attachments: bool = False, log=print) -> dict:
    """完整同步流程：调用爬虫抓取 -> 文章入库 -> 附件下载解析。

    附件下载策略：转专业类文章始终下载；其他分类仅在 all_attachments=True
    时下载（控制请求量）。
    """
    log("① 抓取官网列表与正文……")
    items = psych_crawler.scrape(pages=pages, with_content=True, log=log)

    con = get_db()
    stat = {"new": 0, "updated": 0, "unchanged": 0, "attachments_ok": 0,
            "attachments_pending": 0}
    try:
        log("② 文章写入知识库……")
        for n in items:
            st = upsert_article(con, n)
            stat[st] += 1
            article_id = con.execute(
                "SELECT id FROM articles WHERE href = ?", (n["href"],)).fetchone()["id"]

            enabled = all_attachments or n["category"] == "转专业"
            before = con.execute(
                "SELECT COUNT(*) c FROM attachments WHERE article_id=? AND status='ok'",
                (article_id,)).fetchone()["c"]
            ok = sync_attachments(con, article_id, n.get("attachments", []),
                                  enabled, log=log)
            stat["attachments_ok"] += max(0, ok - before)
            pending = con.execute(
                "SELECT COUNT(*) c FROM attachments WHERE article_id=? AND status='pending'",
                (article_id,)).fetchone()["c"]
            stat["attachments_pending"] += pending
        con.commit()
    finally:
        con.close()
    return stat


# ===========================================================================
# 六、检索接口
# ===========================================================================
def search(con: sqlite3.Connection, query: str, category: str = None,
           limit: int = 30) -> list:
    """全文检索。

    查询词 ≥3 个字符时走 FTS5 trigram 索引（按相关度 bm25 排序）；
    1~2 个字符（如"面试"）trigram 无法匹配，退化为 LIKE 模糊查询。
    多个词用空格分隔时，按"或"关系命中任意一个即可。
    """
    q = (query or "").strip()
    params = []
    cat_sql = ""
    if category:
        cat_sql = " AND a.category = ?"
        params.append(category)

    if len(q) >= 3:
        # FTS 语法：每个词作为短语，双引号需要转义为两个双引号
        phrases = q.split()
        match_expr = " OR ".join('"' + p.replace('"', '""') + '"' for p in phrases)
        sql = (
            "SELECT a.*, bm25(articles_fts) AS rank "
            "FROM articles_fts f JOIN articles a ON a.id = CAST(f.article_id AS INTEGER) "
            "WHERE articles_fts MATCH ?" + cat_sql +
            " ORDER BY rank LIMIT ?"
        )
        rows = con.execute(sql, [match_expr] + params + [limit]).fetchall()
    else:
        like = f"%{q}%"
        sql = (
            "SELECT a.* FROM articles a WHERE ("
            "a.title LIKE ? OR a.content LIKE ? OR EXISTS("
            "  SELECT 1 FROM attachments t WHERE t.article_id=a.id AND t.text LIKE ?))"
            + cat_sql + " ORDER BY a.date DESC LIMIT ?"
        )
        rows = con.execute(sql, [like, like, like] + params + [limit]).fetchall()
    return rows


def recent(con: sqlite3.Connection, category: str = None, limit: int = 20) -> list:
    """按日期倒序列出文章。"""
    sql = "SELECT * FROM articles"
    params = []
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(limit)
    return con.execute(sql, params).fetchall()


def transfer_facts(con: sqlite3.Connection, fact_type: str = None) -> list:
    """取出转专业类文章的关键要素（按文章日期倒序、要素顺序）。

    用来把不同学年的通知并排对比：报名时间、绩点门槛、名额如何变化。
    """
    sql = (
        "SELECT a.date, a.title, a.href, a.category, f.fact_type, f.fact_value, f.context "
        "FROM key_facts f JOIN articles a ON a.id = f.article_id "
        "WHERE a.category = '转专业'"
    )
    params = []
    if fact_type:
        sql += " AND f.fact_type = ?"
        params.append(fact_type)
    sql += " ORDER BY a.date DESC, f.id"
    return con.execute(sql, params).fetchall()


def stats(con: sqlite3.Connection) -> dict:
    """库存统计。"""
    def scalar(sql, *p):
        return con.execute(sql, p).fetchone()[0]
    return {
        "articles": scalar("SELECT COUNT(*) FROM articles"),
        "with_content": scalar("SELECT COUNT(*) FROM articles WHERE content != ''"),
        "unread": scalar("SELECT COUNT(*) FROM articles WHERE is_read = 0"),
        "attachments_total": scalar("SELECT COUNT(*) FROM attachments"),
        "attachments_ok": scalar("SELECT COUNT(*) FROM attachments WHERE status='ok'"),
        "attachments_pending": scalar("SELECT COUNT(*) FROM attachments WHERE status='pending'"),
        "facts": scalar("SELECT COUNT(*) FROM key_facts"),
    }


def mark_all_read(con: sqlite3.Connection) -> int:
    """把所有文章标记为已读（P2 的"必读确认"也会调用），返回更新条数。"""
    cur = con.execute("UPDATE articles SET is_read = 1 WHERE is_read = 0")
    con.commit()
    return cur.rowcount


# ===========================================================================
# 七、命令行入口
# ===========================================================================
def _print_article_rows(rows: list) -> None:
    """打印文章列表（search/recent 共用）。"""
    if not rows:
        print("（没有匹配的记录）")
        return
    for r in rows:
        flag = "  " if r["is_read"] else "● "
        print(f"{flag}[{r['date']}] [{r['category']}] {r['title']}")
        print(f"    {r['href']}")


def reextract_facts(con: sqlite3.Connection) -> tuple:
    """用当前的抽取规则，对库内已有正文重新抽取要素（不重新联网、不删文章）。

    用于改进抽取规则后刷新结果。返回 (处理文章数, 新要素条数)。
    """
    rows = con.execute("SELECT id, content, category FROM articles WHERE content != ''").fetchall()
    total = 0
    for r in rows:
        con.execute("DELETE FROM key_facts WHERE article_id = ?", (r["id"],))
        for fact_type, value, ctx in extract_facts(r["content"], r["category"] or ""):
            con.execute(
                "INSERT OR IGNORE INTO key_facts(article_id, fact_type, fact_value, context)"
                " VALUES(?, ?, ?, ?)",
                (r["id"], fact_type, value, ctx),
            )
            total += 1
    con.commit()
    return len(rows), total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="心理系本地情报知识库")
    sub = ap.add_subparsers(dest="cmd")

    p_sync = sub.add_parser("sync", help="抓取官网并入库")
    p_sync.add_argument("--pages", type=int, default=1, help="每个栏目抓几页")
    p_sync.add_argument("--all-attachments", action="store_true",
                        help="下载解析所有分类的附件（默认只下载转专业类）")

    p_search = sub.add_parser("search", help="全文检索")
    p_search.add_argument("query")
    p_search.add_argument("--category", default=None)

    p_facts = sub.add_parser("facts", help="查看转专业关键要素对比")
    p_facts.add_argument("--type", default=None,
                         help="只看某类要素，如 报名时间/绩点门槛/招生名额/面试地点")

    sub.add_parser("stats", help="库存统计")

    p_recent = sub.add_parser("recent", help="按日期列出最新文章")
    p_recent.add_argument("--category", default=None)
    p_recent.add_argument("--limit", type=int, default=20)

    sub.add_parser("read", help="把全部文章标记为已读")

    sub.add_parser("refacts", help="不联网，用最新规则重新抽取库内文章的要素")

    args = ap.parse_args(argv)
    con = get_db()
    try:
        if args.cmd == "sync":
            st = sync(pages=args.pages, all_attachments=args.all_attachments)
            print("-" * 50)
            print(f"新增 {st['new']} 篇，更新 {st['updated']} 篇，无变化 {st['unchanged']} 篇")
            print(f"附件：本次新解析 {st['attachments_ok']} 个，待下载 {st['attachments_pending']} 个")
            s = stats(con)
            print(f"库内现有：文章 {s['articles']} 篇（含正文 {s['with_content']}），"
                  f"附件 {s['attachments_ok']}/{s['attachments_total']} 已解析，"
                  f"要素 {s['facts']} 条")
        elif args.cmd == "search":
            _print_article_rows(search(con, args.query, args.category))
        elif args.cmd == "facts":
            rows = transfer_facts(con, args.type)
            if not rows:
                print("（还没有抽取出转专业要素，请先运行 sync）")
            cur_title = None
            for r in rows:
                if r["title"] != cur_title:
                    cur_title = r["title"]
                    print(f"\n■ [{r['date']}] {r['title']}")
                    print(f"  {r['href']}")
                print(f"  · {r['fact_type']}：{r['fact_value']}")
                if r["context"] and r["fact_type"] in ("申请条件", "材料清单"):
                    print(f"    原句：{r['context'][:120]}…")
        elif args.cmd == "stats":
            for k, v in stats(con).items():
                print(f"{k:22}: {v}")
        elif args.cmd == "recent":
            _print_article_rows(recent(con, args.category, args.limit))
        elif args.cmd == "read":
            n = mark_all_read(con)
            print(f"已将 {n} 篇标记为已读")
        elif args.cmd == "refacts":
            n_art, n_fact = reextract_facts(con)
            print(f"已对 {n_art} 篇正文重新抽取要素，共 {n_fact} 条")
        else:
            ap.print_help()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
