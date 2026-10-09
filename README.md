# ACLClouds 自动续期

为 [aclclouds.com/dashboard/projects](https://aclclouds.com/dashboard/projects) 上的免费 Minecraft / VPS 服务器自动续期。

> ⚠️ 2026-09 站点改版: 面板已从 `dash.aclclouds.com` 迁到 `aclclouds.com/dashboard`,
> `dash.aclclouds.com` 所有请求 302 到主域 (重定向会丢 Cookie 导致 401)。
> 本仓库脚本已适配, 请务必从 **aclclouds.com** 复制 Cookie。

## 工作原理

- 直接调用 Pelican 风格 API: `POST /api/client/servers/{id}/upgrade/renew` (改版后路由仍存在)
- 通过 Cookie 注入保持登录态 (无需浏览器 / 无需代理 / 不触发 Turnstile)
- 默认剩余时间 < 48h 自动续期
- 每天 UTC 01:00 跑一次 (Actions 自带 cron), 可手动 Run workflow
- 脚本入口统一为 `renew_fixed.py` (`renew.py` 为其兼容别名)

## 部署步骤

### 1. Fork / Clone 本仓库到你的 GitHub

### 2. 获取 Cookie

1. 用浏览器登录 <https://aclclouds.com/dashboard> (⚠️ 不是 dash.aclclouds.com)
2. 按 `F12` 打开开发者工具 → `Application` (或 `存储` / `Storage`) → `Cookies` → `https://aclclouds.com`
3. 复制全部 Cookie 为一个字符串 (格式: `key1=value1; key2=value2; ...`)
> 必须包含这两个关键 Cookie: `XSRF-TOKEN` 和 `__Host-aclclouds_session`
>
> 推荐用浏览器扩展 **EditThisCookie** / **Cookie-Editor** 一键导出 → "导出为 Header 字符串"

### 3. 配置 GitHub Secrets

进入仓库 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`:

| Secret 名称 | 必填 | 说明 |
| --- | --- | --- |
| `ACL_COOKIES` | ✅ 兜底 | 单账号 Cookie, 格式: XSRF-TOKEN=<第1步复制的值>; __Host-aclclouds_session=<第2步复制的值> |
| `ACL_ACCOUNTS` | 多账号 | 格式: `name1\|\|\|cookie1\nname2\|\|\|cookie2` (每行一个) |
| `ACL_EMAIL` + `ACL_PASSWORD` | 免换Cookie | aclclouds 登录邮箱/密码, 开启**浏览器自动登录** |
| `NODE_LINK` | 浏览器登录 | 住宅/原生代理节点 (过 Turnstile 需要干净 IP) |
| `GH_TOKEN` | 自动更新 | GitHub PAT, 登录后自动把新 Cookie 写回 `ACL_COOKIES` |
| `TG_BOT_TOKEN` | TG 通知 | Telegram Bot Token |
| `TG_CHAT_ID` | TG 通知 | 接收通知的 Chat ID |

> 登录优先级: 有 `ACL_COOKIES` 先用 API (快, 不启浏览器); 若 401 且配置了 `ACL_EMAIL`/`ACL_PASSWORD`,
> 自动用浏览器登录重试续期, 成功后经 `GH_TOKEN` 更新 Secret —— **之后不用再手动换 Cookie**。

### 4. 手动测试

进入仓库 → `Actions` → `AclClouds-卡卡自动续期` → `Run workflow`

## 续期规则 (改版后, 面板内置)

| 服务类型 | 可续期阈值 |
| --- | --- |
| 免费 Minecraft | 到期前 2 小时 |
| 免费服务 (普通) | 按周期续期 (如每 4 天 / 每 6h), 面板字段 `can_renew` |
| 付费服务 | 4 天前 |

改版后服务器响应新增字段: `expires_at` / `can_renew` / `free_renewals_remaining` / `free_renewals_max` / `plan.renewal_days`。

脚本逻辑:
1. 若 `can_renew=false` → 跳过 (免费续期次数用完或未到窗口)
2. 否则按 `expires_at` 剩余时间 < 48h 尝试续期
3. 后端返回 `renewNotAvailableYet` 等错误会原样记录, 不影响其他服务器

## 本地调试

```bash
pip install -r requirements.txt
export ACL_COOKIES="XSRF-TOKEN=...; __Host-aclclouds_session=..."
export TG_BOT_TOKEN="可选"
export TG_CHAT_ID="可选"
export DEBUG=1        # 打印每个请求的原始响应
python renew_fixed.py
```

## 错误排查

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| 401 | Cookie 过期 / 从 dash 复制的旧会话 / BASE_URL 指到 dash | 重新登录 aclclouds.com/dashboard 复制新 Cookie; `ACL_BASE_URL` 保持 `https://aclclouds.com` |
| 403 | Cloudflare 风控或 Turnstile | 换网络环境重试; 数据中心 IP 容易被风控 |
| 404 | 服务器 id 用了完整 UUID | 已自动截取短标识 (uuid 前 8 位); 仍 404 则说明该服务器已删除 |
| 续期提示 renewNotAvailableYet | 未到续期窗口 | 正常行为, 无需处理 |

## 维护

- Cookie 有效期约 7-30 天, 过期后重新登录复制即可
- 后端接口变更: 修改 `renew_fixed.py` 中的 `renew_server_api()` 函数
- 想改阈值: 修改 `RENEW_THRESHOLD_HOURS` 环境变量
