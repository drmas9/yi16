/**
 * 冷启动器 · 免费云中转站（Cloudflare Worker + KV）
 * ------------------------------------------------------------------
 * 两个职责：
 *   1) 手机/电脑的共享数据箱（原有功能）
 *      GET  /doc?key=密钥   —— 读取云端保存的一份合并数据
 *      POST /doc?key=密钥   —— 覆盖写入一份合并数据（body 为 JSON）
 *      GET  /health         —— 探活，返回 ok
 *   2) 定时提醒（新增）
 *      每分钟从 GitHub 数据箱读取任务，发现到点的 remind_at 就推送到飞书群机器人。
 *      已发送过的提醒记在 KV 里，不会重复推送。
 *
 * ============ 部署步骤（在 Cloudflare 免费账号里做）============
 * 1. 打开 https://dash.cloudflare.com 注册/登录（免费）。
 * 2. 左侧进入 "Workers & Pages" → "Create" → "Create Worker"，随便起个名字
 *    （例如 coldstart-sync），先点 "Deploy"。
 * 3. 进入这个 Worker → "Edit code"（编辑代码），把本文件全部内容粘进去替换，
 *    注意把下面的 SYNC_KEY 改成你自己的密钥（随便一串字母数字，越乱越好），
 *    然后 "Deploy"（部署）。
 * 4. 给 Worker 绑定一个 KV 存储：
 *    Worker → "Settings" → "Bindings" → "Add" → 选 "KV Namespace"，
 *    Variable name 填：COLD_DATA
 *    KV namespace：选 "Create new namespace"，名字随意（例如 coldstart）。
 *    保存。
 * 5. 回到 Worker 首页，复制它的访问地址，形如：
 *    https://coldstart-sync.你的子域.workers.dev
 * 6. 把这个地址和上面的 SYNC_KEY 分别填进手机端和电脑端即可。
 *
 * ============ 定时提醒额外配置 ============
 * A. 飞书群机器人：
 *    飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址。
 * B. 在 Worker 的 Settings → Variables and Secrets 添加：
 *      GITHUB_TOKEN    GitHub 令牌（和 sync_config.json 里的一致即可，需要 repo 读写）
 *      GITHUB_OWNER    例如 drmas9
 *      GITHUB_REPO     例如 yi16
 *      GITHUB_PATH     默认 data/sync.json
 *      GITHUB_BRANCH   默认 main
 *      FEISHU_WEBHOOK  飞书机器人 Webhook 地址
 *    （GITHUB_* 不填则用下面的默认值）
 * C. 在 Worker 的 Settings → Triggers → Cron Triggers 添加：* * * * *
 *    （每分钟触发一次；免费版每天 10 万次请求，足够用）
 * ==================================================================
 */

// ===== 请把这里改成你自己的密钥（要跟后面两端填的完全一致）=====
const SYNC_KEY = "@sry1610";
// 也支持在 Worker 的环境变量里配置 SYNC_KEY 覆盖上值（可选）

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = env.SYNC_KEY || SYNC_KEY;

    // 预检请求（手机端 POST JSON 会先发这个）
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    if (url.pathname === "/health") {
      return json({ ok: true });
    }

    // 手动触发一次提醒检查（方便调试）：GET /check-reminders?key=密钥
    if (url.pathname === "/check-reminders") {
      if (url.searchParams.get("key") !== key) {
        return json({ error: "密钥不正确" }, 401);
      }
      const result = await checkReminders(env);
      return json(result);
    }

    // 校验密钥
    if (url.searchParams.get("key") !== key) {
      return json({ error: "密钥不正确" }, 401);
    }

    if (url.pathname !== "/doc") {
      return json({ error: "接口不存在" }, 404);
    }

    if (request.method === "GET") {
      const raw = await env.COLD_DATA.get("doc");
      // 云端为空时返回一个空文档，方便首次同步
      return new Response(raw || '{"agile":[],"track":[],"deleted_agile":[],"deleted_track":[],"version":0}', {
        headers: { "Content-Type": "application/json; charset=utf-8", ...CORS },
      });
    }

    if (request.method === "POST") {
      let doc;
      try {
        doc = await request.json();
      } catch (e) {
        return json({ error: "请求体不是合法 JSON" }, 400);
      }
      if (!doc || typeof doc !== "object") {
        return json({ error: "数据格式不正确" }, 400);
      }
      const version = Number(doc.version || 0) + 1;
      doc.version = version;
      doc.updated_at = Date.now();
      await env.COLD_DATA.put("doc", JSON.stringify(doc));
      return json({ ok: true, version });
    }

    return json({ error: "不支持的方法" }, 405);
  },

  // Cron Trigger：每分钟执行一次（在 Worker 设置里添加 * * * * *）
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      checkReminders(env)
        .then((r) => console.log("提醒检查结果:", JSON.stringify(r)))
        .catch((e) => console.error("提醒检查异常:", e && e.message ? e.message : String(e)))
    );
  },
};

/* ==================== 定时提醒 ==================== */

function cfgOf(env) {
  return {
    token: env.GITHUB_TOKEN || "",
    owner: env.GITHUB_OWNER || "drmas9",
    repo: env.GITHUB_REPO || "yi16",
    path: env.GITHUB_PATH || "data/sync.json",
    branch: env.GITHUB_BRANCH || "main",
  };
}

function b64ToUtf8(b64) {
  const bin = atob(String(b64 || "").replace(/\s/g, ""));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

/* 读取 GitHub 数据箱；没有文件时返回空文档 */
async function readSyncDoc(env) {
  const c = cfgOf(env);
  if (!c.token) throw new Error("未配置 GITHUB_TOKEN");
  const url = `https://api.github.com/repos/${c.owner}/${c.repo}/contents/${c.path}?ref=${encodeURIComponent(c.branch)}&_=${Date.now()}`;
  const resp = await fetch(url, {
    headers: {
      Authorization: "Bearer " + c.token,
      Accept: "application/vnd.github+json",
      "User-Agent": "coldstart-reminder",
    },
  });
  if (resp.status === 404) return {};
  if (!resp.ok) throw new Error("读取数据箱失败 HTTP " + resp.status);
  const body = await resp.json();
  try {
    return JSON.parse(b64ToUtf8(body.content)) || {};
  } catch (e) {
    return {};
  }
}

/* 推送一条文本消息到飞书群机器人 */
async function sendFeishu(env, text) {
  if (!env.FEISHU_WEBHOOK) throw new Error("未配置 FEISHU_WEBHOOK");
  const resp = await fetch(env.FEISHU_WEBHOOK, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ msg_type: "text", content: { text } }),
  });
  const data = await resp.json().catch(() => ({}));
  // 飞书返回 code=0 才算成功（部分版本用 StatusCode）
  if (data.code != null && data.code !== 0) {
    throw new Error("飞书推送失败：" + (data.msg || data.code));
  }
  return true;
}

function fmtTime(ms) {
  // 固定按北京时间（UTC+8）显示
  const d = new Date(ms + 8 * 3600 * 1000);
  const p = (n) => (n < 10 ? "0" : "") + n;
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

/* 检查到期提醒：读数据箱 → 找 remind_at <= now 且未发送过的 → 推飞书 */
async function checkReminders(env) {
  const now = Date.now();
  let doc;
  try {
    doc = await readSyncDoc(env);
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
  if (!env.FEISHU_WEBHOOK) return { ok: false, error: "未配置 FEISHU_WEBHOOK" };

  const items = [];
  (doc.agile || []).forEach((it) => {
    if (it && it.id && +it.remind_at > 0) items.push({ kind: "敏捷任务", title: it.text, id: it.id, at: +it.remind_at });
  });
  (doc.track || []).forEach((it) => {
    if (it && it.id && +it.remind_at > 0) items.push({ kind: "长期任务", title: it.name, id: it.id, at: +it.remind_at });
  });

  let sent = 0;
  const details = [];
  for (const it of items) {
    // 只提醒刚到期 30 分钟内的，避免把历史遗留的旧提醒全部翻出来
    if (it.at > now || it.at < now - 30 * 60 * 1000) continue;
    const dedupeKey = `sent:${it.id}:${it.at}`;
    if (await env.COLD_DATA.get(dedupeKey)) continue;

    const text = `⏰ 任务提醒\n【${it.kind}】${it.title || "(无标题)"}\n提醒时间：${fmtTime(it.at)}`;
    try {
      await sendFeishu(env, text);
      await env.COLD_DATA.put(dedupeKey, "1", { expirationTtl: 60 * 60 * 24 * 30 });
      sent++;
      details.push({ id: it.id, title: it.title, at: it.at });
    } catch (e) {
      details.push({ id: it.id, error: String(e.message || e) });
    }
  }
  return { ok: true, checked: items.length, sent, details };
}