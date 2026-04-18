# Vercel 部署详细教程

本教程面向将本项目部署到 Vercel 的场景，目标是让你从 0 到可用完成以下能力：

- 对外提供 TIS 和 Blackboard 的 iCal 订阅链接
- 使用 Vercel Cron 自动刷新缓存
- 使用 Vercel KV 作为持久化缓存（推荐）

## 1. 先理解 Vercel 部署特性

在 Vercel 上本项目运行于 Python Serverless Function。和 Docker 常驻服务相比有几个关键差异：

1. 运行实例不是常驻进程，函数按请求触发。
2. 本地文件系统是临时的，不适合长期保存 SQLite 数据。
3. 最稳妥方案是使用 Vercel KV 保存缓存数据。
4. Vercel Cron 通过请求你的 API 路径触发同步任务。

结论：Vercel 场景推荐 `SCHEDULE_STORAGE_MODE=kv`。

## 2. 前置准备

你需要准备：

1. 一个可用的 GitHub 仓库（已推送本项目）。
2. 一个 Vercel 账号。
3. 你的南科大账号与密码。
4. 一个随机长串令牌用于日历订阅鉴权（ICAL_TOKEN）。
5. 一个随机长串令牌用于 Cron 鉴权（CRON_SECRET）。

可本地生成令牌：

```bash
openssl rand -hex 32
```

## 3. 核心配置文件说明

项目根目录已提供 [vercel.json](../vercel.json)，关键内容是：

1. `app.py` 使用 `@vercel/python` 构建。
2. 所有路由转发到 `app.py`。
3. Cron 每天按 `0 2 * * *` 触发 `/api/cron/fetch?source=all`。

注意：Vercel Cron 的时区是 UTC。
`0 2 * * *` 等价于北京时间每天 10:00。

## 4. 在 Vercel 导入项目

1. 打开 Vercel 控制台。
2. 点击 `Add New...` -> `Project`。
3. 选择你的 GitHub 仓库并导入。
4. Framework Preset 选择 `Other`（或保持自动识别）。
5. Root Directory 保持仓库根目录。
6. 直接点击 `Deploy`（环境变量后续补齐也可）。

## 5. 配置环境变量（必须）

在项目设置 `Settings -> Environment Variables` 中添加：

1. `SUSTECH_SID`：你的学号。
2. `SUSTECH_PASSWORD`：你的 CAS 密码。
3. `ICAL_TOKEN`：用于订阅链接鉴权。
4. `CRON_SECRET`：用于 Cron Bearer 鉴权。
5. `SCHEDULE_STORAGE_MODE`：设置为 `kv`。
6. `CAS_USE_QR_LOGIN`：建议 `false`（Vercel 场景建议密码登录）。
7. `CAS_QR_ALLOW_PASSWORD_FALLBACK`：建议 `true`。
8. `LOCATION_PREFIX`：可选。
9. `COURSE_NAME_FILTER`：可选，JSON 数组字符串，例如 `["创新实践"]`。

## 6. 绑定 Vercel KV（强烈建议）

1. 进入项目 `Storage`。
2. 创建并连接 `KV`。
3. Vercel 会自动注入 KV 相关环境变量。
4. 确认生产环境中可见 KV 变量后，重新部署一次。

## 7. 重新部署并验证

环境变量配置完成后执行一次重新部署：

1. 进入 `Deployments`。
2. 选择最新部署，点击 `Redeploy`。

部署成功后先做健康检查：

```bash
curl -i https://<your-vercel-domain>/
```

预期返回 JSON，至少包含 `status: ok`。

## 8. 手动触发一次同步（首轮初始化）

首次部署后缓存为空，先手动触发一次 Cron 接口：

```bash
curl -i \
  -H "Authorization: Bearer <CRON_SECRET>" \
  "https://<your-vercel-domain>/api/cron/fetch?source=all"
```

如果返回中 `tis` 和 `bb` 为 `success`，说明缓存初始化成功。

## 9. 验证 iCal 订阅链接

TIS：

```text
https://<your-vercel-domain>/tis/schedule.ics?token=<ICAL_TOKEN>
```

Blackboard：

```text
https://<your-vercel-domain>/blackboard/schedule.ics?token=<ICAL_TOKEN>
```

在浏览器打开应返回 `.ics` 文本；再导入你的日历客户端测试自动订阅。

## 10. 常见问题排查

### 10.1 `/api/cron/fetch` 返回 401

检查：

1. 请求头是否带了 `Authorization: Bearer <CRON_SECRET>`。
2. Vercel 中 `CRON_SECRET` 是否拼写正确。
3. 修改变量后是否重新部署。

### 10.2 订阅链接返回 404（数据尚不可用）

说明缓存还没成功写入。

处理：

1. 先手动触发一次同步接口。
2. 查看函数日志确认 TIS/BB 是否抓取成功。
3. 确认 KV 已连接且 `SCHEDULE_STORAGE_MODE=kv`。

### 10.4 健康检查里 `kv_client_available=false`

当前实现优先使用 `upstash_redis.Redis.from_env()`，并兼容 Vercel KV 变量映射（`KV_REST_API_URL` / `KV_REST_API_TOKEN`）。

检查顺序：

1. 确认部署安装了依赖 `upstash-redis`（本仓库 `requirements.txt` 已包含）。
2. 确认项目已连接 KV，且函数环境中存在 `KV_REST_API_URL` 与 `KV_REST_API_TOKEN`。
3. 重新部署后查看函数日志中的 `[kv]` 前缀信息，定位是 `upstash_redis` 初始化失败还是回退到 `vercel_kv`。

### 10.3 Blackboard 间歇性 500

这是上游服务偶发问题，项目中已加入预热、重试与分片兜底。

建议：

1. 保留 Cron 自动重试刷新策略。
2. 遇到单次失败可稍后重试。
3. 对关键截止时间仍以 Blackboard 官网为准。

## 11. 生产建议

1. `ICAL_TOKEN` 与 `CRON_SECRET` 使用高强度随机值并定期轮换。
2. 不要在日志中打印任何真实凭据。
3. 使用自定义域名后，确认 TLS 与访问控制策略生效。
4. 监控 Vercel Function 错误率，必要时调整 Cron 时段避开高峰。

## 12. Vercel 与 Docker 方案如何选择

1. 如果你要极简托管和自动 HTTPS：优先 Vercel。
2. 如果你依赖容器常驻行为（例如长期后台线程日志观察）：优先 Docker。
3. 如果你同时有公开访问和本地运维诉求，也可以双方案并行。
