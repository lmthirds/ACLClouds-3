#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds 自动续期脚本 - 适配 2026-09 aclclouds.com 改版
========================================================
改版后关键变化 (均已适配):
1. 面板从 dash.aclclouds.com 迁到 aclclouds.com/dashboard,
   dash.aclclouds.com 所有请求 302 -> aclclouds.com。
   => BASE_URL 必须指向 https://aclclouds.com
   (否则 requests 跨域重定向时会丢弃手动注入的 Cookie 头 -> 401)
2. 服务器响应新增字段: expires_at / can_renew / free_renewals_remaining /
   free_renewals_max / plan.renewal_days / is_free / service_type
3. 续期接口仍为 POST /api/client/servers/{id}/upgrade/renew
   (未登录时返回 401 而非 404, 说明路由存在)
4. 免费 Minecraft 提前 2 小时可续; 免费服务按周期(每4天/每6h等);
   付费服务提前 4 天。

用法:
    export ACL_COOKIES="XSRF-TOKEN=...; __Host-aclclouds_session=..."
    export TG_BOT_TOKEN=...  TG_CHAT_ID=...   # 可选
    export DEBUG=1                             # 可选, 打印原始响应
    python renew_fixed.py

浏览器自动登录 (免手动换 Cookie):
    export ACL_EMAIL=you@example.com ACL_PASSWORD=xxx
    export IS_PROXY=true PROXY_SERVER=socks5://127.0.0.1:1080
    需要 seleniumbase; 过 CF Turnstile 需要干净代理。
"""

import os
import sys
import json
import time
import re
import urllib.parse
from datetime import datetime, timezone

import requests

# ==================== 配置 ====================
# 改版后 API 直接由 aclclouds.com 提供; 不要再指到 dash.aclclouds.com
# (dash 会 302 到主域, 重定向时 Cookie 会被 requests 丢弃导致 401)
BASE_URL = os.environ.get("ACL_BASE_URL", "https://aclclouds.com").rstrip("/")
RENEW_THRESHOLD_HOURS = float(os.environ.get("RENEW_THRESHOLD_HOURS", "48"))

# Cookie: 完整的浏览器 Cookie 字符串, 必须来自 https://aclclouds.com
# 至少包含 XSRF-TOKEN 和 __Host-aclclouds_session
COOKIE = os.environ.get("ACL_COOKIES", "").strip()

# 多账号支持 (可选), 格式: name1|||cookie1\nname2|||cookie2
MULTI_ACCOUNTS = os.environ.get("ACL_ACCOUNTS", "").strip()

# TG 通知
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

# 调试: 打印每个请求的原始响应
DEBUG = os.environ.get("DEBUG", "0") == "1"

# ==================== 浏览器自动登录 (可选, 免手动换 Cookie) ====================
# 配置 ACL_EMAIL + ACL_PASSWORD 后, Cookie 过期时脚本会用浏览器自动登录
# (过 Turnstile 需要干净代理, 与 bot-hosting 方案一致)
ACL_EMAIL = os.environ.get("ACL_EMAIL", "").strip()
ACL_PASSWORD = os.environ.get("ACL_PASSWORD", "").strip()
# Discord OAuth 回退登录 (账号需绑定 Discord; Turnstile 组件加载不出时自动使用)
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "socks5://127.0.0.1:1080"
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()          # 自动更新 ACL_COOKIES Secret
GH_REPO = os.environ.get("GH_REPO", "weikkadd/ACLClouds-server").strip()

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 登录表单选择器 (覆盖 Pterodactyl/Persepolis 各版本 + 本地化 placeholder)
EMAIL_SELECTORS = (
    'input[name="email"]', 'input[type="email"]', 'input[autocomplete="email"]',
    'input[name="login"]', 'input[name="identifier"]', 'input[name="username"]',
    'input[placeholder*="example.com" i]', 'input[placeholder*="email" i]',
    'input[placeholder*="邮箱"]', 'input[id="email"]',
)
PASSWORD_SELECTORS = (
    'input[type="password"]', 'input[name="password"]',
    'input[autocomplete="current-password"]', 'input[placeholder*="password" i]',
    'input[placeholder*="密码"]', 'input[id="password"]',
)


# ==================== 工具函数 ====================
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg):
    print(msg, flush=True)


def debug(msg):
    if DEBUG:
        print(f"  🐛 {msg}", flush=True)


def send_tg(text):
    """发送 TG 通知 (纯文本, 避免 Markdown 特殊字符解析失败); 返回是否成功"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("ℹ️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID, 已跳过 TG 通知")
        log("   GitHub Actions: Settings → Secrets and variables → Actions 添加这两个 secret")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=15,
        )
        if r.status_code == 200:
            log("📨 TG 通知已发送")
            return True
        log(f"❌ TG 发送失败 HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        log(f"⚠️ TG 推送异常: {e}")
        return False


def fmt_remaining(seconds):
    if seconds is None:
        return "?"
    if seconds < 0:
        return "已过期"
    seconds = int(seconds)
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def parse_iso(s):
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ==================== API 会话 (纯 HTTP) ====================
def sanitize_cookie(cookie_str):
    """清洗 Cookie 字符串, 保证可以作为 HTTP header 发送

    常见坑 (会触发 requests 的 Invalid header value 异常):
    - 复制时带了换行/回车
    - 误粘贴了 curl 的 "Cookie: ..." 前缀
    - 值带了引号或非 latin-1 字符 (中文/emoji)
    返回清洗后的 "; " 分隔字符串。
    """
    s = (cookie_str or "").strip()
    if not s:
        return ""
    # 去掉误粘贴的 "Cookie:" 前缀
    if s.lower().startswith("cookie:"):
        s = s[len("cookie:"):].strip()
    # 去掉两端包裹的引号
    if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
        s = s[1:-1]
    # 折叠所有空白/控制字符 (换行、回车、制表符等)
    s = re.sub(r"\s+", " ", s)

    parts = []
    for kv in s.split(";"):
        kv = kv.strip()
        if not kv or "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        # 丢弃含非法字符的条目 (非 latin-1 或控制字符), 避免 header 校验失败
        try:
            b = v.encode("latin-1")
        except UnicodeEncodeError:
            log(f"⚠️ Cookie 条目 {k} 含非 latin-1 字符, 已丢弃 (请重新复制)")
            continue
        if any(c < 0x20 or c == 0x7f for c in b):
            log(f"⚠️ Cookie 条目 {k} 含控制字符, 已丢弃 (请重新复制)")
            continue
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def build_api_session(cookie_str):
    """构建 API session, 原样保留 __Host- 前缀 Cookie

    关键: 不再用 s.cookies.set (cookiejar 会剥离 __Host- 前缀,
    且 prepare_cookies 会用剥离后的名字重建 Cookie 头导致服务器不识别),
    而是保存原始字符串, 在每次请求时强制覆盖 Cookie 头。
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/dashboard",
    })
    s._raw_cookie = sanitize_cookie(cookie_str)
    return s


def get_xsrf(session):
    """从原始 Cookie 字符串提取 XSRF-TOKEN (Laravel 加密值, URL 解码后放 header)"""
    raw = getattr(session, '_raw_cookie', '')
    if raw:
        m = re.search(r'(?:^|;\s*)XSRF-TOKEN=([^;]+)', raw)
        if m:
            return urllib.parse.unquote(m.group(1).strip())
    return None


def _send(session, method, path, payload=None):
    """发送请求: 注入 X-XSRF-TOKEN 头 + 强制覆盖原始 Cookie 头"""
    headers = {}
    token = get_xsrf(session)
    if token:
        headers["X-XSRF-TOKEN"] = token

    req = requests.Request(method, f"{BASE_URL}{path}",
                           headers=headers, json=payload)
    prepared = session.prepare_request(req)
    # prepare_cookies 会删掉手动 Cookie 头, 这里写回原样 (含 __Host- 前缀)
    if getattr(session, '_raw_cookie', None):
        prepared.headers['Cookie'] = session._raw_cookie
    r = session.send(prepared, timeout=30)

    # 重定向防护: 如果响应 302 且跳去了别的域, Cookie 已被 requests 丢弃,
    # 手动把 Location 和目标打出来, 方便定位"BASE_URL 指错域"问题
    if r.is_redirect:
        debug(f"{method} {path} -> 302 {r.headers.get('Location')} (重定向后 Cookie 会被丢弃, 检查 ACL_BASE_URL)")

    debug(f"{method} {path} -> HTTP {r.status_code} | {r.text[:300]}")
    return r


def api_get(session, path):
    return _send(session, "GET", path)


def api_post(session, path, payload=None):
    return _send(session, "POST", path, payload or {})


# ==================== 数据解析 ====================
def list_servers(session):
    """拉取服务器列表 (改版后仍为 GET /api/client)"""
    r = api_get(session, "/api/client")
    if r.status_code == 401:
        log(f"❌ 登录失败 (401): Cookie 无效或过期")
        log(f"   请重新登录 https://aclclouds.com/dashboard 并复制新 Cookie")
        return []
    if r.status_code != 200:
        log(f"❌ /api/client 返回 HTTP {r.status_code}: {r.text[:300]}")
        return []
    j = r.json()
    if isinstance(j, dict):
        return j.get("data", [])
    return j if isinstance(j, list) else []


def server_detail(session, sid):
    r = api_get(session, f"/api/client/servers/{sid}")
    if r.status_code != 200:
        debug(f"server_detail {sid} -> HTTP {r.status_code}")
        return None
    try:
        j = r.json()
        return j.get("attributes", j) if isinstance(j, dict) else j
    except Exception:
        return None


def extract_server_id(attrs):
    """提取续期接口需要的短标识 (Pterodactyl 短 id = uuid 前 8 位)

    改版后 attributes 里可能只有 uuid (完整 UUID), 直接用完整 UUID 调
    /api/client/servers/{id} 会得到 404, 这里自动截取前 8 位。
    """
    if not isinstance(attrs, dict):
        return None
    for k in ("identifier", "uuid_short", "short_uuid", "server_id"):
        v = attrs.get(k)
        if v:
            return str(v)
    u = attrs.get("uuid") or attrs.get("id")
    if u:
        u = str(u)
        if "-" in u:          # 完整 UUID 形如 xxxxxxxx-xxxx-...
            return u.split("-")[0]
        return u[:8]
    return None


def find_expire(attrs, detail=None):
    """从多个可能字段找到期时间 (改版后主字段为 expires_at)"""
    candidates = []
    if attrs:
        candidates.append(attrs)
    if detail:
        candidates.append(detail)
    for c in list(candidates):
        rel = c.get("relationships") if isinstance(c, dict) else None
        if rel:
            candidates.append(rel)
    for c in candidates:
        if not isinstance(c, dict):
            continue
        for key in ("expires_at", "expire_at", "renew_at", "renewable_at",
                    "expiration_date", "expires", "expiry"):
            v = c.get(key)
            if v:
                return key, v
    return None, None


def renewal_availability(attrs, detail=None):
    """读取改版后的续期状态字段

    返回 (can_renew, free_remaining, reason):
      - can_renew: True/False/None(旧结构, 无此字段)
      - free_remaining: 剩余免费续期次数 (可能为 None)
      - reason: 不可续期的原因文本
    """
    for c in (attrs, detail):
        if not isinstance(c, dict):
            continue
        if c.get("can_renew") is not None:
            can = bool(c.get("can_renew"))
            free = c.get("free_renewals_remaining")
            reason = None
            if not can:
                if free == 0:
                    reason = "免费续期次数已用完"
                else:
                    reason = "面板标记暂不可续期 (can_renew=false)"
            return can, free, reason
    return None, None, None


def renew_server_api(session, sid):
    """调用续期接口 (路由已确认仍存在)"""
    r = api_post(session, f"/api/client/servers/{sid}/upgrade/renew")
    captcha_required = False
    if r.status_code == 403:
        body = r.text
        try:
            j = r.json()
            code = j.get("code") if isinstance(j, dict) else None
            if code == "captcha_required" or "captcha" in body.lower():
                captcha_required = True
        except Exception:
            if "captcha" in body.lower():
                captcha_required = True
    return r, captcha_required


def renew_error_msg(r):
    """从续期响应中提取后端错误 (可能 2xx 但 body 带错误, 如 renewNotAvailableYet)"""
    try:
        j = r.json()
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    # Pterodactyl/Laravel 错误格式: { errors: [ { code, detail } ] }
    errors = j.get("errors")
    if isinstance(errors, list) and errors:
        e = errors[0]
        return f"{e.get('code', '')}: {e.get('detail', '')}".strip(": ")
    if j.get("code"):
        return f"{j.get('code')}: {j.get('detail', '')}".strip(": ")
    return None


# ==================== 浏览器自动登录 (SeleniumBase, 免手动换 Cookie) ====================
def _cf_challenge(sb):
    """检测是否处于 Cloudflare 验证页 (该页没有真实登录表单, 只有 CF 挑战 UI)"""
    try:
        url = (sb.get_current_url() or "").lower()
        if "/cdn-cgi" in url or "challenge" in url:
            return True
    except Exception:
        pass
    try:
        title = (sb.get_title() or "").lower()
        if any(k in title for k in ("just a moment", "attention required",
                                     "checking your browser", "verify you are")):
            return True
    except Exception:
        pass
    try:
        src = (sb.get_page_source() or "")[:5000].lower()
        if "cf-challenge" in src or "challenge-platform" in src:
            return True
    except Exception:
        pass
    return False


def _dump_login_form(sb):
    """诊断: 打印登录页 CF 状态 / URL / 可见表单元素, 便于排障"""
    url = title = ""
    cf = False
    try:
        url = sb.get_current_url()
        title = sb.get_title() or ""
        cf = _cf_challenge(sb)
    except Exception:
        pass
    print(f"🔍 登录表单诊断 | CF验证: {'是' if cf else '否'} | title: {title[:60]}")
    print(f"   URL: {url}")
    try:
        els = sb.execute_script("""
            (function(){
                return Array.from(document.querySelectorAll('input,button'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => el.tagName + '|name=' + (el.getAttribute('name')||'')
                          + '|type=' + (el.getAttribute('type')||'')
                          + '|ph=' + (el.getAttribute('placeholder')||'')
                          + '|' + ((el.innerText||el.textContent||'').trim().slice(0,25)))
                    .slice(0, 25);
            })()
        """)
        if els:
            print("   可见 input/button:")
            for x in els:
                print("   ", x)
        else:
            print("   ⚠️ 无可见 input/button — CF 验证页 / SPA 未渲染 / 表单结构已变")
            try:
                body_text = (sb.get_text("body") or "").strip().replace("\n", " ")[:300]
                print("   body 片段:", body_text)
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ 枚举登录表单失败: {e}")


def _click_turnstile(sb):
    """切换到 Turnstile 复选框 iframe 直接点击 (无头/无显示器环境可用)

    返回是否找到并点击。uc_gui_click_captcha() 依赖真实屏幕坐标,
    在 GitHub Actions (无显示器) 下会一直返回 False。
    """
    try:
        sb.driver.switch_to.default_content()
        iframes = sb.driver.find_elements("css selector", "iframe")
        for ifr in iframes:
            src = (ifr.get_attribute("src") or "") + " " + (ifr.get_attribute("title") or "")
            if any(k in src.lower() for k in ("challenges.cloudflare.com", "turnstile", "checkbox")):
                sb.driver.switch_to.frame(ifr)
                ok = False
                for js in (
                    "var c=document.querySelector('input[type=checkbox]');if(c){c.click();true}else false",
                    "var e=document.querySelector('label,div[role=checkbox],.rc-anchor');if(e){e.click();true}else false",
                ):
                    try:
                        if sb.driver.execute_script(js):
                            ok = True
                            break
                    except Exception:
                        continue
                sb.driver.switch_to.default_content()
                return ok
    except Exception:
        pass
    return False


def _discord_oauth_login(sb):
    """Discord OAuth 登录回退 (账号需绑定 Discord; Turnstile 组件加载不出时自动使用)

    流程: 点登录页 Discord 按钮 → 抓 authorize URL (client_id/state/redirect_uri)
    → 用 Discord token 完成授权拿 code → 浏览器打开回调 → 提取 cookie。
    """
    if not DISCORD_TOKEN:
        log("ℹ️ 未配置 DISCORD_TOKEN, 跳过 Discord OAuth 回退")
        return None
    log("🔑 尝试 Discord OAuth 登录 (账号需绑定 Discord)...")
    try:
        # 1) 点击登录页的 Discord 按钮
        clicked = False
        for sel in ('a:contains("Discord")', 'button:contains("Discord")',
                    'a[href*="discord.com/oauth2"]', '[data-provider="discord"]'):
            try:
                if sb.is_element_visible(sel):
                    sb.click(sel)
                    clicked = True
                    log(f"✅ 已点击 Discord 登录按钮: {sel}")
                    break
            except Exception:
                continue
        if not clicked:
            log("❌ 未找到 Discord 登录按钮")
            return None

        # 2) 等待跳转到 discord.com 并抓取 authorize URL
        auth_url = None
        for _ in range(20):
            url = sb.get_current_url()
            if "discord.com/oauth2/authorize" in url or "discord.com/api/oauth2" in url:
                auth_url = url
                break
            sb.sleep(1)
        if not auth_url:
            log(f"❌ 未跳转到 Discord, 当前 URL: {sb.get_current_url()[:120]}")
            try:
                sb.save_screenshot("acl_discord_no_redirect.png")
            except Exception:
                pass
            return None
        log(f"📝 Discord authorize URL: {auth_url[:160]}")

        params = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        client_id = (params.get("client_id") or [""])[0]
        state = (params.get("state") or [""])[0]
        redirect_uri = (params.get("redirect_uri") or [""])[0]
        scope = (params.get("scope") or [""])[0]
        if not (client_id and state and redirect_uri):
            log("❌ authorize URL 缺少参数 (client_id/state/redirect_uri)")
            return None
        log(f"   client_id={client_id} | redirect={redirect_uri[:60]} | scope={scope or '(默认)'}")

        # 3) 用 Discord token 请求授权, 拿带 code 的回调 URL
        #    GET 若返回 200 (同意授权页) 则改 POST 自动授权 (authorize:true)
        disc_url = ("https://discord.com/api/v9/oauth2/authorize?"
                    + urllib.parse.urlencode({
                        "client_id": client_id,
                        "response_type": "code",
                        "redirect_uri": redirect_uri,
                        "scope": scope or "identify email guilds",
                        "state": state,
                    }))
        dheaders = {
            "Authorization": DISCORD_TOKEN,
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"),
        }

        def _disc_authorize(method="GET"):
            if method == "POST":
                payload = {
                    "permissions": "0",
                    "authorize": True,
                    "integration_type": 0,
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": redirect_uri,
                    "scope": scope or "identify email guilds",
                    "state": state,
                }
                return requests.post(disc_url, headers={**dheaders, "Content-Type": "application/json"},
                                     json=payload, allow_redirects=False, timeout=25)
            return requests.get(disc_url, headers=dheaders, allow_redirects=False, timeout=25)

        try:
            resp = _disc_authorize("GET")
            if resp.status_code not in (301, 302):
                log(f"ℹ️ GET 授权返回 HTTP {resp.status_code} (可能需要同意授权), 尝试 POST 自动授权...")
                resp = _disc_authorize("POST")
        except Exception as e:
            log(f"❌ Discord 授权请求异常: {e}")
            return None

        callback = ""
        if resp.status_code in (301, 302):
            callback = resp.headers.get("Location", "")
        else:
            # 个别情况下 200 响应体里带 location 字段
            try:
                j = resp.json()
                callback = j.get("location") or ""
            except Exception:
                callback = ""
        if not callback:
            log(f"❌ Discord 授权失败: HTTP {resp.status_code} - {resp.text[:200]}")
            return None
        log(f"✅ 拿到回调 URL: {re.sub(r'code=[^&]+', 'code=***', callback)[:120]}")

        # 4) 浏览器打开回调完成登录
        sb.uc_open_with_reconnect(callback, reconnect_time=4)
        sb.sleep(3)
        logged_in = False
        for _ in range(40):
            url = sb.get_current_url()
            if "auth/login" not in url and "login" not in url.lower():
                logged_in = True
                break
            sb.sleep(1)
        if not logged_in:
            log(f"❌ OAuth 回调后未跳转, 当前: {sb.get_current_url()[:120]}")
            try:
                print("   📝 页面内容:", sb.get_text("body")[:200])
            except Exception:
                pass
            return None
        log(f"✅ Discord OAuth 登录成功: {sb.get_current_url()[:80]}")

        # 5) 提取 cookie
        cookies = sb.get_cookies()
        xsrf = session = None
        for c in cookies:
            if c.get("name") == "XSRF-TOKEN":
                xsrf = c.get("value")
            if c.get("name") == "__Host-aclclouds_session":
                session = c.get("value")
        if not (xsrf and session):
            log("❌ 登录成功但未拿到 cookie")
            print("   cookie 名:", [c.get("name") for c in cookies])
            return None
        cookie = f"XSRF-TOKEN={xsrf}; __Host-aclclouds_session={session}"
        log(f"✅ Discord OAuth 登录完成, 已获取新 Cookie (session 前 8 位: {session[:8]}...)")
        return cookie
    except Exception as e:
        log(f"💥 Discord OAuth 异常: {e}")
        return None


def browser_login():
    """用浏览器登录 aclclouds.com, 返回 Cookie 字符串; 失败返回 None

    流程: 打开 /auth/login → 等表单渲染 (兼容 CF 验证) → 填账密 → 过 Turnstile → 提交 → 取 cookie
    需要: pip install seleniumbase; 过 Turnstile 需要干净代理 (IS_PROXY=true)。
    """
    if not ACL_EMAIL or not ACL_PASSWORD:
        log("ℹ️ 未配置 ACL_EMAIL/ACL_PASSWORD, 跳过浏览器自动登录")
        return None
    try:
        from seleniumbase import SB
    except ImportError:
        log("❌ 未安装 seleniumbase, 无法浏览器登录 (pip install seleniumbase)")
        return None

    kwargs = {"uc": True, "headless": HEADLESS}
    if IS_PROXY:
        log(f"🔗 浏览器走代理: {PROXY_SERVER}")
        kwargs["proxy"] = PROXY_SERVER
    else:
        log("🌐 浏览器直连 (Turnstile 过不去时请设 IS_PROXY=true + 干净代理)")

    log("🚀 启动浏览器登录 aclclouds.com ...")
    try:
        with SB(**kwargs) as sb:
            sb.open("https://aclclouds.com/auth/login")
            sb.wait_for_ready_state_complete()

            # ===== 轮询等待登录表单出现 (兼容 CF 验证页 + SPA 慢渲染), 最长 60s =====
            email_sel = pw_sel = None
            deadline = time.time() + 60
            while time.time() < deadline:
                cur_url = sb.get_current_url()
                log(f"📝 当前 URL: {cur_url}")

                if _cf_challenge(sb):
                    log("🛡️ 检测到 Cloudflare 验证页, 等待通过...")
                    sb.sleep(5)
                    continue

                if email_sel is None:
                    for sel in EMAIL_SELECTORS:
                        try:
                            if sb.is_element_visible(sel):
                                email_sel = sel
                                break
                        except Exception:
                            continue

                if pw_sel is None:
                    for sel in PASSWORD_SELECTORS:
                        try:
                            if sb.is_element_visible(sel):
                                pw_sel = sel
                                break
                        except Exception:
                            continue

                if email_sel and pw_sel:
                    log(f"✅ 找到登录表单: 邮箱={email_sel} 密码={pw_sel}")
                    break

                elapsed = int(60 - (deadline - time.time()))
                log(f"⏳ 表单未出现 (已等 {elapsed}s / 60s)")
                _dump_login_form(sb)
                sb.sleep(3)

            if not (email_sel and pw_sel):
                log("❌ 60s 内未找到邮箱/密码输入框")
                log("   可能原因: CF 验证未通过 / 登录页结构已变 / 代理 IP 被风控")
                _dump_login_form(sb)
                try:
                    sb.save_screenshot("acl_login_form.png")
                    with open("acl_login_form.html", "w", encoding="utf-8") as f:
                        f.write(sb.get_page_source())
                    print("   📸 已保存 acl_login_form.png + .html 供排障")
                except Exception:
                    pass
                return None

            # 填写邮箱 + 密码
            sb.type(email_sel, ACL_EMAIL)
            log(f"✅ 已填写邮箱: {email_sel}")
            sb.type(pw_sel, ACL_PASSWORD)
            log(f"✅ 已填写密码: {pw_sel}")

            # Turnstile: 先等组件 iframe 出现, 再点击直到 token 真正生成
            log("🔒 尝试通过 Turnstile 验证...")

            def _page_iframes():
                """原生 driver 枚举 iframe (sb.execute_script 在登录页返回值不可靠)"""
                try:
                    sb.driver.switch_to.default_content()
                    frs = sb.driver.find_elements("css selector", "iframe")
                    return [(f.get_attribute("src") or "") + " " + (f.get_attribute("title") or "")
                            for f in frs]
                except Exception:
                    return []

            def _captcha_passed():
                try:
                    sb.driver.switch_to.default_content()
                    tok = sb.driver.execute_script(
                        "var el=document.querySelector('textarea[name=\\"cf-turnstile-response\\"],"
                        "input[name=\\"cf-turnstile-response\\"]');"
                        "return !!(el&&el.value&&el.value.length>10);")
                    if tok:
                        return True
                except Exception:
                    pass
                try:
                    pg = sb.get_page_source().lower()
                    return not any(k in pg for k in
                                   ("i am not a robot", "captcha incorrect", "secured by aclclouds"))
                except Exception:
                    return True

            # 1) 等 Turnstile iframe 出现 (最多 30s)
            widget_seen = False
            for _ in range(15):
                frs = _page_iframes()
                if frs and any(("cloudflare" in x.lower()) or ("turnstile" in x.lower()) for x in frs):
                    log(f"🔍 检测到 Turnstile iframe: {[x[:70] for x in frs]}")
                    widget_seen = True
                    break
                sb.sleep(2)
            if not widget_seen:
                log("⚠️ 30 秒内未出现 Turnstile iframe (组件被拦或未加载)")
                log("   改用 Discord OAuth 登录回退...")
                return _discord_oauth_login(sb)

            # 2) 点击复选框直到 token 生成
            turnstile_ok = False
            for attempt in range(1, 5):
                clicked = False
                try:
                    clicked = sb.uc_gui_click_captcha()
                except Exception as e:
                    log(f"   ⚠️ uc 点击异常: {e}")
                if not clicked:
                    # 无真实显示器时 GUI 点击不可用, 改 iframe 直接点击
                    clicked = _click_turnstile(sb)
                log(f"   第 {attempt} 次点击验证框: {'已点击' if clicked else '未找到'}")
                sb.sleep(8)
                if _captcha_passed():
                    turnstile_ok = True
                    log("✅ Turnstile 验证已通过 (响应 token 已生成)")
                    break
                log(f"   ⏳ 第 {attempt} 次后仍未通过, 重试...")
            if not turnstile_ok:
                log("❌ Turnstile 验证未通过 (IP 可能被 CF 风控)")
                log("   改用 Discord OAuth 登录回退...")
                return _discord_oauth_login(sb)

            # 提交登录 + 等待跳转 (验证码失败时自动重试一轮)
            logged_in = False
            for cycle in range(1, 3):
                submit_ok = False
                for sel in ('button[type="submit"]',
                            'button:contains("Sign in")', 'button:contains("Login")',
                            'button:contains("Se connecter")', 'button:contains("Connexion")',
                            'button:contains("登录")', 'button:contains("Sign In")'):
                    try:
                        if sb.is_element_visible(sel):
                            sb.click(sel)
                            submit_ok = True
                            log(f"✅ 已点击登录按钮: {sel}")
                            break
                    except Exception:
                        continue
                if not submit_ok:
                    log("⚠️ 未找到登录提交按钮, 尝试回车提交")
                    try:
                        sb.enter()
                    except Exception:
                        pass

                # 等待跳转离开登录页
                for _ in range(40):
                    url = sb.get_current_url()
                    if "auth/login" not in url and "login" not in url.lower():
                        logged_in = True
                        break
                    sb.sleep(1)
                if logged_in:
                    break

                # 检查是否验证码错误, 是则重新点验证框再提交一轮
                try:
                    body = sb.get_text("body") or ""
                except Exception:
                    body = ""
                if "captcha" in body.lower():
                    log(f"⏳ 第 {cycle} 次提交后提示验证码错误, 重新点击验证框并提交...")
                    try:
                        sb.uc_gui_click_captcha()
                    except Exception:
                        pass
                    sb.sleep(7)
                    continue
                break

            if not logged_in:
                log("❌ 登录后未跳转 (可能 2FA 或验证码未过)")
                try:
                    print("   📝 页面内容:", sb.get_text("body")[:300])
                except Exception:
                    pass
                try:
                    sb.save_screenshot("acl_login_failed.png")
                except Exception:
                    pass
                return None

            log(f"✅ 登录成功, 当前页面: {sb.get_current_url()}")

            # 提取 cookie
            cookies = sb.get_cookies()
            xsrf = session = None
            for c in cookies:
                if c.get("name") == "XSRF-TOKEN":
                    xsrf = c.get("value")
                if c.get("name") == "__Host-aclclouds_session":
                    session = c.get("value")
            if not (xsrf and session):
                log("❌ 登录成功但未拿到 XSRF-TOKEN/__Host-aclclouds_session")
                print("   cookie 名:", [c.get("name") for c in cookies])
                return None
            cookie = f"XSRF-TOKEN={xsrf}; __Host-aclclouds_session={session}"
            log(f"✅ 浏览器登录完成, 已获取新 Cookie (session 前 8 位: {session[:8]}...)")

            # 验证新 Cookie 是否可用 (防止拿到脏会话)
            try:
                vs = build_api_session(cookie)
                vr = api_get(vs, "/api/client")
                if vr.status_code == 200:
                    log("✅ 新 Cookie 验证通过 (/api/client -> 200)")
                else:
                    log(f"⚠️ 新 Cookie 自检: /api/client -> HTTP {vr.status_code} (仍返回 Cookie, 请手动确认)")
            except Exception as ve:
                log(f"⚠️ Cookie 自检异常: {ve}")
            return cookie
    except Exception as e:
        log(f"💥 浏览器登录异常: {e}")
        return None


def update_acl_secret(cookie_str):
    """用 gh CLI 把新 Cookie 写回 GitHub Secret ACL_COOKIES (Actions 里 gh 已认证)"""
    if not GH_TOKEN:
        log("ℹ️ 未配置 GH_TOKEN, 不更新 GitHub Secret (本地下次仍需浏览器登录)")
        return False
    import subprocess
    masked = (cookie_str[:20] + "..." + cookie_str[-10:]) if len(cookie_str) > 30 else "***"
    log(f"🔄 更新 Secret ACL_COOKIES (新值: {masked})")
    try:
        env = os.environ.copy()
        env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", "ACL_COOKIES", "--repo", GH_REPO, "--body", cookie_str],
            capture_output=True, text=True, timeout=60, check=False, env=env,
        )
        if proc.returncode == 0:
            log("✅ ACL_COOKIES 更新成功")
            return True
        stderr = proc.stderr.strip()
        if "403" in stderr or "forbidden" in stderr.lower():
            log(f"❌ 更新失败 (403 无权限): {stderr[:200]}")
            log("   请确认 GH_TOKEN 有 secrets 写权限 (PAT 需 admin:repo 权限, 或 workflow 加 secrets: write)")
        else:
            log(f"❌ 更新失败: {stderr[:200]}")
        return False
    except Exception as e:
        log(f"❌ 更新 Secret 异常: {e}")
        return False


# ==================== 单账号续期流程 ====================
def process_account(label, cookie_str):
    log(f"\n{'='*60}")
    log(f"👤 账号: {label}")
    log(f"🌐 站点: {BASE_URL}")
    log(f"{'='*60}")

    if not cookie_str:
        return {"label": label, "ok": False, "msg": "Cookie 为空", "renewed": 0, "failed": 0}

    # 友好提示: Cookie 里的 __Host- 域名与 BASE_URL 是否匹配
    if "dash.aclclouds.com" in BASE_URL:
        log("⚠️ ACL_BASE_URL 指向 dash.aclclouds.com, 该域名现已 302 到 aclclouds.com")
        log("   (重定向会丢 Cookie 导致 401), 建议改回 https://aclclouds.com")

    session = build_api_session(cookie_str)

    # 1. 登录自检
    try:
        test_r = api_get(session, "/api/client")
    except Exception as e:
        log(f"❌ 网络异常: {e}")
        return {"label": label, "ok": False, "msg": f"网络异常: {e}", "renewed": 0, "failed": 0}

    if test_r.status_code == 200:
        log("✅ API 登录验证通过")
    elif test_r.status_code == 401:
        log("❌ 登录失败: HTTP 401")
        log("   原因排查:")
        log("   1) Cookie 过期 -> 重新登录 https://aclclouds.com/dashboard 复制新 Cookie")
        log("   2) Cookie 是从 dash.aclclouds.com 复制的旧会话 -> 必须从 aclclouds.com 复制")
        log("   3) ACL_BASE_URL 指向了 dash.aclclouds.com -> 改回 https://aclclouds.com")
        log(f"   响应: {test_r.text[:300]}")
        return {"label": label, "ok": False, "msg": "登录失败 HTTP 401 (Cookie 无效/过期/域名不匹配)", "renewed": 0, "failed": 0}
    elif test_r.status_code == 403:
        log("❌ 登录失败: HTTP 403")
        log("   IP 被 Cloudflare 风控或需要 Turnstile 验证 (本机/数据中心 IP 常见)")
        return {"label": label, "ok": False, "msg": "登录失败 HTTP 403 (CF 风控/Turnstile)", "renewed": 0, "failed": 0}
    else:
        log(f"❌ 登录失败: HTTP {test_r.status_code}")
        log(f"   响应: {test_r.text[:300]}")
        return {"label": label, "ok": False, "msg": f"登录失败 HTTP {test_r.status_code}", "renewed": 0, "failed": 0}

    # 2. 拉取服务器列表
    servers = list_servers(session)
    if not servers:
        log("📦 没有服务器")
        return {"label": label, "ok": True, "msg": "无服务器", "renewed": 0, "failed": 0}

    log(f"📦 共 {len(servers)} 台服务器")
    if servers:
        first = servers[0]
        attrs = first.get("attributes", first) if isinstance(first, dict) else {}
        debug(f"首台服务器字段: {list(attrs.keys()) if isinstance(attrs, dict) else type(attrs).__name__}")

    now = datetime.now(timezone.utc)
    to_renew = []
    skipped = []

    for idx, srv in enumerate(servers, 1):
        attrs = srv.get("attributes", srv) if isinstance(srv, dict) else {}
        sid = extract_server_id(attrs)
        name = attrs.get("name", f"server-{idx}")
        if not sid:
            debug(f"{name}: 缺少 server id, attrs keys={list(attrs.keys()) if isinstance(attrs, dict) else '?'}")
            skipped.append(f"⚠️ {name}: 缺少 server id")
            continue

        # 到期时间 (列表没有则拉详情)
        _, expire_str = find_expire(attrs)
        detail = None
        if not expire_str:
            detail = server_detail(session, sid)
            if detail:
                _, expire_str = find_expire(attrs, detail)

        # 改版后的续期状态字段
        can_renew, free_left, reason = renewal_availability(attrs, detail)
        # 面板自动续期开关 (改版新增): auto_renew=true 时由面板自己续, 脚本跳过
        auto_renew = None
        for c in (attrs, detail):
            if isinstance(c, dict) and c.get("auto_renew") is not None:
                auto_renew = bool(c.get("auto_renew"))
                break
        if auto_renew:
            log(f"  ⏭️ {name}: 面板已开启自动续期 (auto_renew=true), 无需脚本处理")
            skipped.append(f"⏭️ {name}: auto_renew=true (面板自动续期)")
            continue
        if can_renew is False:
            log(f"  ⏭️ {name}: {reason or '不可续期'}")
            skipped.append(f"⏭️ {name}: {reason or '不可续期'}")
            continue
        if can_renew is True and free_left == 0:
            log(f"  ⏭️ {name}: 免费续期次数已用完 (free_renewals_remaining=0)")
            skipped.append(f"⏭️ {name}: 免费续期次数已用完")
            continue

        if not expire_str:
            log(f"  ⏭️ {name}: 无到期时间字段 (attrs keys 见 DEBUG=1)")
            skipped.append(f"⚠️ {name}: 无到期时间字段")
            continue

        expire = parse_iso(expire_str)
        if not expire:
            log(f"  ⏭️ {name}: 到期时间格式错误 ({expire_str})")
            skipped.append(f"⚠️ {name}: 到期时间格式错误")
            continue

        remaining = (expire - now).total_seconds()
        remaining_h = remaining / 3600
        log(f"  - {name}: 到期 {expire_str} | 剩余 {fmt_remaining(remaining)} ({remaining_h:.1f}h)"
            + (f" | 免费续期余 {free_left}" if free_left is not None else ""))

        if remaining_h < RENEW_THRESHOLD_HOURS:
            to_renew.append({"id": sid, "name": name, "remaining": remaining})
        else:
            skipped.append(f"⏭️ {name}: 剩 {fmt_remaining(remaining)}, 未到阈值 {RENEW_THRESHOLD_HOURS:g}h")

    if not to_renew:
        msg = f"所有服务器剩余时间充足, 无需续期" if not skipped else f"无需续期 ({len(skipped)} 台跳过)"
        log(f"ℹ️ {msg}")
        return {"label": label, "ok": True, "msg": msg, "renewed": 0, "failed": 0, "results": skipped}

    log(f"🔄 需要续期 {len(to_renew)} 台服务器")

    # 3. 执行续期
    renewed = 0
    failed = 0
    results = list(skipped)

    for srv in to_renew:
        log(f"\n🖥️ 续期: {srv['name']} (id={srv['id']})")
        try:
            r, captcha = renew_server_api(session, srv["id"])

            if r.status_code == 404:
                # 大概率是 id 类型不对 (完整 uuid 而非短标识)
                log(f"❌ HTTP 404: 服务器 id 可能不是短标识 ({srv['id']})")
                results.append(f"❌ {srv['name']}: 404 (id 格式问题)")
                failed += 1
            elif r.status_code == 401:
                log(f"❌ HTTP 401: 会话失效, 本次不再继续")
                results.append(f"❌ {srv['name']}: 401 会话失效")
                failed += 1
                break
            elif r.status_code == 403 and captcha:
                log(f"🛡️ 需要 Turnstile 验证 (纯 API 无法通过, 跳过)")
                results.append(f"🛡️ {srv['name']}: 需要 Turnstile 验证")
                failed += 1
            elif r.status_code in (200, 201, 202, 204):
                # 2xx 也可能 body 带错误 (如 renewNotAvailableYet)
                err = renew_error_msg(r)
                if err:
                    log(f"⏭️ 后端拒绝: {err}")
                    results.append(f"⏭️ {srv['name']}: {err}")
                    # 未真正续期, 但也不算失败
                else:
                    time.sleep(1.5)
                    new_detail = server_detail(session, srv["id"])
                    _, new_expire_str = find_expire({}, new_detail)
                    new_expire = parse_iso(new_expire_str) if new_expire_str else None
                    if new_expire:
                        new_remaining = (new_expire - now).total_seconds()
                        results.append(f"✅ {srv['name']}: {fmt_remaining(srv['remaining'])} → {fmt_remaining(new_remaining)}")
                        log(f"✅ 续期成功: {fmt_remaining(srv['remaining'])} → {fmt_remaining(new_remaining)}")
                    else:
                        results.append(f"✅ {srv['name']}: 续期成功")
                        log(f"✅ 续期成功")
                    renewed += 1
            else:
                body = r.text[:200]
                err = renew_error_msg(r)
                results.append(f"❌ {srv['name']}: HTTP {r.status_code} {err or body}")
                failed += 1
                log(f"❌ 续期失败: HTTP {r.status_code} {err or body}")
        except Exception as e:
            results.append(f"❌ {srv['name']}: {e}")
            failed += 1
            log(f"❌ 异常: {e}")

        time.sleep(2)

    return {
        "label": label,
        "ok": failed == 0,
        "msg": f"成功 {renewed} 台, 失败 {failed} 台",
        "renewed": renewed,
        "failed": failed,
        "results": results,
    }


# ==================== 主入口 ====================
def collect_accounts():
    """返回 [(label, cookie_str), ...]"""
    accounts = []

    if MULTI_ACCOUNTS:
        for line in MULTI_ACCOUNTS.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|||" in line:
                name, ck = line.split("|||", 1)
                accounts.append((name.strip(), ck.strip()))
            else:
                accounts.append((f"account-{len(accounts)+1}", line))

    if not accounts and COOKIE:
        accounts.append(("main", COOKIE))

    return accounts


def build_summary(all_results):
    """构建 TG 汇总消息"""
    renewed_total = sum(r.get("renewed", 0) for r in all_results)
    failed_total = sum(r.get("failed", 0) for r in all_results)

    lines = ["🎮 ACLClouds 自动续期", f"⏰ {now_str()}", ""]
    lines.append(f"📊 ✅ {renewed_total} | ❌ {failed_total}")
    lines.append("")

    for r in all_results:
        if not r.get("ok"):
            lines.append(f"👤 {r['label']}: ❌ {r.get('msg', '失败')}")
        else:
            lines.append(f"👤 {r['label']}: ✅ {r.get('msg', '成功')}")
            if r.get("results"):
                for res in r["results"]:
                    lines.append(f"  {res}")
        lines.append("")

    return "\n".join(lines)


def main():
    log(f"🚀 ACLClouds 续期脚本启动 @ {now_str()}")
    log(f"🌐 站点: {BASE_URL}")
    log(f"⏰ 续期阈值: {RENEW_THRESHOLD_HOURS:g}h")

    if "dash.aclclouds.com" in BASE_URL:
        log("⚠️ 检测到 ACL_BASE_URL=dash.aclclouds.com, 该域名已废弃(302->aclclouds.com)")
        log("  请使用: export ACL_BASE_URL=https://aclclouds.com")

    accounts = collect_accounts()

    # 没有 Cookie 但有账密 → 直接用浏览器登录获取
    if not accounts and ACL_EMAIL and ACL_PASSWORD:
        log("🔑 未配置 Cookie, 尝试浏览器自动登录...")
        fresh = browser_login()
        if fresh:
            accounts = [("main", fresh)]
            log("✅ 浏览器登录成功, 使用新 Cookie 执行续期")
            if GH_TOKEN:
                update_acl_secret(fresh)
        else:
            msg = ("❌ 浏览器登录失败\n\n"
                   "请检查:\n"
                   "- ACL_EMAIL/ACL_PASSWORD 是否正确\n"
                   "- 代理能否过 Turnstile (IS_PROXY=true + 干净代理)\n"
                   "- 是否触发 2FA (需手动处理)\n"
                   "- 查看 acl_login_form.png / .html 截图排障")
            log(msg)
            send_tg(msg)
            sys.exit(1)

    if not accounts:
        msg = ("❌ 未配置 ACL_COOKIES / ACL_ACCOUNTS / (ACL_EMAIL+ACL_PASSWORD)\n\n"
               "请使用以下环境变量之一:\n"
               "- ACL_COOKIES: 单账号 Cookie (来自 https://aclclouds.com)\n"
               "- ACL_ACCOUNTS: 多账号 (格式: name|||cookie)\n"
               "- ACL_EMAIL + ACL_PASSWORD: 浏览器自动登录 (免手动换 Cookie)")
        log(msg)
        send_tg(msg)
        sys.exit(1)

    log(f"📋 共 {len(accounts)} 个账号")

    all_results = []
    for label, ck in accounts:
        try:
            res = process_account(label, ck)
            # Cookie 登录 401 且有账密 → 浏览器自动登录重试 (免手动换 Cookie)
            if (not res.get("ok")) and ACL_EMAIL and ACL_PASSWORD and "401" in str(res.get("msg", "")):
                log("🔑 Cookie 登录 401, 尝试浏览器自动登录重试...")
                fresh = browser_login()
                if fresh:
                    log("✅ 浏览器登录成功, 用新 Cookie 重试续期")
                    res = process_account(label, fresh)
                    if GH_TOKEN:
                        update_acl_secret(fresh)
                else:
                    log("⚠️ 浏览器登录失败, 维持 401 结果")
                    log("   请手动登录 https://aclclouds.com/dashboard 复制新 Cookie 更新 ACL_COOKIES")
        except Exception as e:
            res = {"label": label, "ok": False, "msg": f"异常: {e}", "renewed": 0, "failed": 1}
        all_results.append(res)

    summary = build_summary(all_results)
    print("\n" + summary + "\n")
    send_tg(summary)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n用户中断")
    except Exception as e:
        log(f"💥 未捕获异常: {e}")
        send_tg(f"🎮 ACLClouds 续期\n\n💥 脚本崩溃: {e}")
        sys.exit(1)
