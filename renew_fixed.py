#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds 自动续期脚本 - 适配 2026-09 aclclouds.com 改版 (优化版)
========================================================
改版后关键变化 (均已适配):
1. 面板从 dash.aclclouds.com 迁到 aclclouds.com/dashboard,
   dash.aclclouds.com 所有请求 302 -> aclclouds.com。
   => BASE_URL 必须指向 https://aclclouds.com
2. 服务器响应新增字段: expires_at / can_renew / free_renewals_remaining /
   free_renewals_max / plan.renewal_days / is_free / service_type
3. 续期接口仍为 POST /api/client/servers/{id}/upgrade/renew
4. 免费 Minecraft 提前 2 小时可续; 免费服务按周期; 付费服务提前 4 天。

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
BASE_URL = os.environ.get("ACL_BASE_URL", "https://aclclouds.com").rstrip("/")
RENEW_THRESHOLD_HOURS = float(os.environ.get("RENEW_THRESHOLD_HOURS", "48"))

# Cookie: 完整的浏览器 Cookie 字符串, 必须来自 https://aclclouds.com
COOKIE = os.environ.get("ACL_COOKIES", "").strip()

# 多账号支持 (可选), 格式: name1|||cookie1\nname2|||cookie2
MULTI_ACCOUNTS = os.environ.get("ACL_ACCOUNTS", "").strip()

# TG 通知
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

# 调试: 打印每个请求的原始响应
DEBUG = os.environ.get("DEBUG", "0") == "1"

# ==================== 浏览器自动登录 (可选, 免手动换 Cookie) ====================
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
    'input[name="username"]', 'input[name="email"]', 'input[type="email"]', 'input[autocomplete="email"]',
    'input[name="login"]', 'input[name="identifier"]',
    'input[placeholder*="example.com" i]', 'input[placeholder*="email" i]',
    'input[placeholder*="邮箱"]', 'input[id="email"]', 'input[id="username"]',
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
    s = (cookie_str or "").strip()
    if not s:
        return ""
    if s.lower().startswith("cookie:"):
        s = s[len("cookie:"):].strip()
    if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
        s = s[1:-1]
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
        try:
            b = v.encode("latin-1")
        except UnicodeEncodeError:
            continue
        if any(c < 0x20 or c == 0x7f for c in b):
            continue
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def build_api_session(cookie_str):
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
    raw = getattr(session, '_raw_cookie', '')
    if raw:
        m = re.search(r'(?:^|;\s*)XSRF-TOKEN=([^;]+)', raw)
        if m:
            return urllib.parse.unquote(m.group(1).strip())
    return None


def _send(session, method, path, payload=None):
    headers = {}
    token = get_xsrf(session)
    if token:
        headers["X-XSRF-TOKEN"] = token

    req = requests.Request(method, f"{BASE_URL}{path}",
                           headers=headers, json=payload)
    prepared = session.prepare_request(req)
    if getattr(session, '_raw_cookie', None):
        prepared.headers['Cookie'] = session._raw_cookie
    r = session.send(prepared, timeout=30)
    return r


def api_get(session, path):
    return _send(session, "GET", path)


def api_post(session, path, payload=None):
    return _send(session, "POST", path, payload or {})


# ==================== 数据解析 ====================
def list_servers(session):
    r = api_get(session, "/api/client")
    if r.status_code == 401:
        log(f"❌ 登录失败 (401): Cookie 无效或过期")
        return []
    if r.status_code != 200:
        return []
    j = r.json()
    if isinstance(j, dict):
        return j.get("data", [])
    return j if isinstance(j, list) else []


def server_detail(session, sid):
    r = api_get(session, f"/api/client/servers/{sid}")
    if r.status_code != 200:
        return None
    try:
        j = r.json()
        return j.get("attributes", j) if isinstance(j, dict) else j
    except Exception:
        return None


def extract_server_id(attrs):
    if not isinstance(attrs, dict):
        return None
    for k in ("identifier", "uuid_short", "short_uuid", "server_id"):
        v = attrs.get(k)
        if v:
            return str(v)
    u = attrs.get("uuid") or attrs.get("id")
    if u:
        u = str(u)
        if "-" in u:
            return u.split("-")[0]
        return u[:8]
    return None


def find_expire(attrs, detail=None):
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
    try:
        j = r.json()
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    errors = j.get("errors")
    if isinstance(errors, list) and errors:
        e = errors[0]
        return f"{e.get('code', '')}: {e.get('detail', '')}".strip(": ")
    if j.get("code"):
        return f"{j.get('code')}: {j.get('detail', '')}".strip(": ")
    return None


# ==================== 浏览器自动登录 (SeleniumBase) ====================
def _cf_challenge(sb):
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
    return False


def _click_turnstile(sb):
    try:
        sb.driver.switch_to.default_content()
        iframes = sb.driver.find_elements("css selector", "iframe")
        for ifr in iframes:
            src = (ifr.get_attribute("src") or "") + " " + (ifr.get_attribute("title") or "")
            if any(k in src.lower() for k in ("challenges.cloudflare.com", "turnstile", "checkbox")):
                sb.driver.switch_to.frame(ifr)
                for js in (
                    "var c=document.querySelector('input[type=checkbox]');if(c){c.click();true}else false",
                    "var e=document.querySelector('label,div[role=checkbox],.rc-anchor');if(e){e.click();true}else false",
                ):
                    try:
                        if sb.driver.execute_script(js):
                            sb.driver.switch_to.default_content()
                            return True
                    except Exception:
                        continue
                sb.driver.switch_to.default_content()
    except Exception:
        pass
    return False


def _discord_oauth_login(sb):
    if not DISCORD_TOKEN:
        log("ℹ️ 未配置 DISCORD_TOKEN，无法使用 Discord OAuth 回退登录。建议在 GitHub Secrets 中配置 DISCORD_TOKEN")
        return None
    log("🔑 尝试 Discord OAuth 登录 (账号需绑定 Discord)...")
    try:
        clicked = False
        for sel in ('a:contains("Discord")', 'button:contains("Discord")',
                    'a[href*="discord.com/oauth2"]', '[data-provider="discord"]'):
            try:
                if sb.is_element_visible(sel):
                    sb.click(sel)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return None

        auth_url = None
        for _ in range(20):
            url = sb.get_current_url()
            if "discord.com/oauth2/authorize" in url:
                auth_url = url
                break
            sb.sleep(1)
        if not auth_url:
            return None

        params = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        client_id = (params.get("client_id") or [""])[0]
        state = (params.get("state") or [""])[0]
        redirect_uri = (params.get("redirect_uri") or [""])[0]
        scope = (params.get("scope") or [""])[0]
        if not (client_id and state and redirect_uri):
            return None

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
            "User-Agent": UA,
        }
        resp = requests.get(disc_url, headers=dheaders, allow_redirects=False, timeout=25)
        if resp.status_code not in (301, 302):
            payload = {
                "permissions": "0", "authorize": True, "integration_type": 0,
                "client_id": client_id, "response_type": "code",
                "redirect_uri": redirect_uri, "scope": scope or "identify email guilds", "state": state,
            }
            resp = requests.post(disc_url, headers={**dheaders, "Content-Type": "application/json"},
                                 json=payload, allow_redirects=False, timeout=25)

        callback = resp.headers.get("Location", "") if resp.status_code in (301, 302) else ""
        if not callback:
            return None

        sb.uc_open_with_reconnect(callback, reconnect_time=4)
        sb.sleep(3)
        cookies = sb.get_cookies()
        xsrf = session = None
        for c in cookies:
            if c.get("name") == "XSRF-TOKEN":
                xsrf = c.get("value")
            if c.get("name") == "__Host-aclclouds_session":
                session = c.get("value")
        if not (xsrf and session):
            return None
        return f"XSRF-TOKEN={xsrf}; __Host-aclclouds_session={session}"
    except Exception as e:
        log(f"💥 Discord OAuth 异常: {e}")
        return None


def browser_login():
    if not ACL_EMAIL or not ACL_PASSWORD:
        return None
    try:
        from seleniumbase import SB
    except ImportError:
        return None

    kwargs = {"uc": True, "headless": HEADLESS}
    if IS_PROXY:
        kwargs["proxy"] = PROXY_SERVER

    log("🚀 启动浏览器登录 aclclouds.com ...")
    try:
        with SB(**kwargs) as sb:
            sb.open("https://aclclouds.com/auth/login")
            sb.wait_for_ready_state_complete()

            email_sel = pw_sel = None
            deadline = time.time() + 60
            while time.time() < deadline:
                if _cf_challenge(sb):
                    sb.sleep(5)
                    continue
                if not email_sel:
                    for sel in EMAIL_SELECTORS:
                        if sb.is_element_visible(sel):
                            email_sel = sel
                            break
                if not pw_sel:
                    for sel in PASSWORD_SELECTORS:
                        if sb.is_element_visible(sel):
                            pw_sel = sel
                            break
                if email_sel and pw_sel:
                    break
                sb.sleep(3)

            if not (email_sel and pw_sel):
                return _discord_oauth_login(sb)

            sb.type(email_sel, ACL_EMAIL)
            sb.type(pw_sel, ACL_PASSWORD)

            # 触发 Turnstile 验证
            for _ in range(5):
                try:
                    sb.uc_gui_click_captcha()
                except Exception:
                    pass
                _click_turnstile(sb)
                sb.sleep(3)

            # 点击提交
            for sel in ('button[type="submit"]', 'button:contains("Login")', 'button:contains("登录")'):
                try:
                    if sb.is_element_visible(sel):
                        sb.click(sel)
                        break
                except Exception:
                    continue

            sb.sleep(5)
            cookies = sb.get_cookies()
            xsrf = session = None
            for c in cookies:
                if c.get("name") == "XSRF-TOKEN":
                    xsrf = c.get("value")
                if c.get("name") == "__Host-aclclouds_session":
                    session = c.get("value")
            if not (xsrf and session):
                return _discord_oauth_login(sb)

            return f"XSRF-TOKEN={xsrf}; __Host-aclclouds_session={session}"
    except Exception as e:
        log(f"💥 浏览器登录异常: {e}")
        return _discord_oauth_login(sb)


def update_acl_secret(cookie_str):
    if not GH_TOKEN:
        return False
    import subprocess
    try:
        env = os.environ.copy()
        env["GH_TOKEN"] = GH_TOKEN
        subprocess.run(
            ["gh", "secret", "set", "ACL_COOKIES", "--repo", GH_REPO, "--body", cookie_str],
            capture_output=True, text=True, timeout=60, check=False, env=env,
        )
        return True
    except Exception:
        return False


# ==================== 单账号续期流程 ====================
def process_account(label, cookie_str):
    log(f"\n{'='*60}")
    log(f"👤 账号: {label}")
    log(f"🌐 站点: {BASE_URL}")
    log(f"{'='*60}")

    if not cookie_str:
        return {"label": label, "ok": False, "msg": "Cookie 为空", "renewed": 0, "failed": 0}

    session = build_api_session(cookie_str)
    test_r = api_get(session, "/api/client")

    if test_r.status_code == 200:
        log("✅ API 登录验证通过")
    elif test_r.status_code == 401:
        log("❌ 登录失败: HTTP 401 (Cookie 无效或过期)")
        return {"label": label, "ok": False, "msg": "登录失败 HTTP 401", "renewed": 0, "failed": 0}
    else:
        return {"label": label, "ok": False, "msg": f"登录失败 HTTP {test_r.status_code}", "renewed": 0, "failed": 0}

    servers = list_servers(session)
    if not servers:
        return {"label": label, "ok": True, "msg": "无服务器", "renewed": 0, "failed": 0}

    now = datetime.now(timezone.utc)
    to_renew = []
    skipped = []

    for idx, srv in enumerate(servers, 1):
        attrs = srv.get("attributes", srv) if isinstance(srv, dict) else {}
        sid = extract_server_id(attrs)
        name = attrs.get("name", f"server-{idx}")
        if not sid:
            continue

        _, expire_str = find_expire(attrs)
        detail = None
        if not expire_str:
            detail = server_detail(session, sid)
            if detail:
                _, expire_str = find_expire(attrs, detail)

        can_renew, free_left, reason = renewal_availability(attrs, detail)
        auto_renew = False
        for c in (attrs, detail):
            if isinstance(c, dict) and c.get("auto_renew") is not None:
                auto_renew = bool(c.get("auto_renew"))
                break

        if auto_renew:
            skipped.append(f"⏭️ {name}: auto_renew=true")
            continue
        if can_renew is False:
            skipped.append(f"⏭️ {name}: {reason or '不可续期'}")
            continue

        if not expire_str:
            continue

        expire = parse_iso(expire_str)
        if not expire:
            continue

        remaining = (expire - now).total_seconds()
        remaining_h = remaining / 3600

        if remaining_h < RENEW_THRESHOLD_HOURS:
            to_renew.append({"id": sid, "name": name, "remaining": remaining})
        else:
            skipped.append(f"⏭️ {name}: 剩 {fmt_remaining(remaining)}, 未到阈值")

    if not to_renew:
        return {"label": label, "ok": True, "msg": "无需续期", "renewed": 0, "failed": 0, "results": skipped}

    renewed = 0
    failed = 0
    results = list(skipped)

    for srv in to_renew:
        try:
            r, captcha = renew_server_api(session, srv["id"])
            if r.status_code in (200, 201, 202, 204):
                err = renew_error_msg(r)
                if err:
                    results.append(f"⏭️ {srv['name']}: {err}")
                else:
                    renewed += 1
                    results.append(f"✅ {srv['name']}: 续期成功")
            else:
                failed += 1
                results.append(f"❌ {srv['name']}: HTTP {r.status_code}")
        except Exception as e:
            failed += 1
            results.append(f"❌ {srv['name']}: {e}")
        time.sleep(2)

    return {
        "label": label,
        "ok": failed == 0,
        "msg": f"成功 {renewed} 台, 失败 {failed} 台",
        "renewed": renewed,
        "failed": failed,
        "results": results,
    }


def main():
    log(f"🚀 ACLClouds 续期脚本启动 @ {now_str()}")
    accounts = []
    if MULTI_ACCOUNTS:
        for line in MULTI_ACCOUNTS.splitlines():
            if "|||" in line:
                name, ck = line.split("|||", 1)
                accounts.append((name.strip(), ck.strip()))
    if not accounts and COOKIE:
        accounts.append(("main", COOKIE))

    if not accounts and ACL_EMAIL and ACL_PASSWORD:
        fresh = browser_login()
        if fresh:
            accounts = [("main", fresh)]
            if GH_TOKEN:
                update_acl_secret(fresh)

    all_results = []
    for label, ck in accounts:
        res = process_account(label, ck)
        if (not res.get("ok")) and ACL_EMAIL and ACL_PASSWORD:
            fresh = browser_login()
            if fresh:
                res = process_account(label, fresh)
                if GH_TOKEN:
                    update_acl_secret(fresh)
        all_results.append(res)

    summary = "\n".join([r.get("msg", "") for r in all_results])
    send_tg(f"🎮 ACLClouds 续期\n\n{summary}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_tg(f"🎮 ACLClouds 续期\n\n💥 脚本崩溃: {e}")
        sys.exit(1)
