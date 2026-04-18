# 南方科技大学日历订阅服务 (TIS 课表 & Blackboard DDL)

这是一个自托管服务，用于抓取你的南科大 TIS 教学服务平台上的**课程表**和 Blackboard 上的**作业/事件截止日期 (DDL)**，并将其转换为两个独立、安全、可自动更新的 iCalendar (.ics) 订阅链接。你可以将这些链接添加到任何支持 iCal 订阅的日历应用中（如 Apple Calendar, Google Calendar, Outlook 等），实现课表与 DDL 的统一管理。

## ✨ 功能特性

- **双日历订阅**:
  - **TIS 课表**: 提供包含上课时间、地点、教师信息的课程表。
  - **Blackboard DDL**: 提供包含课程名称、作业标题和截止时间的事件列表。
- **标准 iCal 格式**: 提供与绝大多数日历应用兼容的 `.ics` 订阅链接。
- **自动更新与缓存**: 服务会自动缓存日历数据，并按设定的周期更新，确保你的日历保持最新，同时最大限度地减少对学校服务器的请求。
- **安全与私密**:
  - **Token 鉴权**: 订阅链接包含一个私有令牌，防止他人未经授权访问你的日历。
  - **自托管**: 你的学号和密码仅存在于你自己的服务器上，不会泄露给任何第三方。
  - **自动 HTTPS**: 通过 Caddy 自动配置 HTTPS，确保数据传输全程加密。
- **轻松部署**: 使用 Docker 和 Docker Compose 完全容器化，一条命令即可完成部署。
- **智能格式化**:
  - 自动转换课程地点简写（如“一教”->“第一教学楼”）。
- **Docker 日志扫码登录**:
  - 容器启动后会在日志中输出可扫描的 ASCII 二维码。
  - 扫码成功后自动缓存 CAS token，并在二维码过期后自动重打。
- **双存储后端（可切换）**:
  - 支持 `Vercel KV`、`SQLite` 或 `双写` 模式（默认双写）。
  - 数据读取可按配置优先走数据库导出 ICS。
- **节假日避退标记**:
  - 节假日课程不会消失，而是导出为 `CANCELLED` 事件并保留说明。

## 🚀 部署指南

### Vercel 部署（Serverless）

如果你希望直接部署到 Vercel，请先阅读详细教程：

- [Vercel 部署详细教程](docs/vercel-deployment.md)

部署此服务需要你有一台拥有公网 IP 的服务器，并为其准备一个域名。

### 准备工作

1.  一台拥有公网 IP 的服务器（例如，任何云服务商的 VPS）。
2.  一个域名，并将其 DNS A/AAAA 记录指向你服务器的公网 IP。
3.  在服务器上安装 [Docker](https://docs.docker.com/engine/install/) 和 [Docker Compose](https://docs.docker.com/compose/install/)。

### 安装步骤

1.  **克隆项目代码**

    ```bash
    git clone https://github.com/your-username/SUSTechReptile.git
    cd SUSTechReptile
    ```

2.  **创建并配置 `.env` 文件**
    此文件用于存放你的敏感信息。如果 `.env.example` 文件不存在，请手动创建 `.env` 文件。

    ```bash
    # 复制模板文件（如果存在）
    # cp .env.example .env
    ```

    然后，编辑 `.env` 文件，填入你的个人信息：

    ```ini
    # .env

    # 你的南科大学号
    SUSTECH_SID=12345678

    # 你的南科大 CAS 登录密码
    SUSTECH_PASSWORD=your_secret_password

    # 用于订阅链接的安全令牌，请设置为一个长且随机的字符串,可以使用`openssl rand -base64 32`生成
    ICAL_TOKEN=

    # Cron 接口鉴权令牌
    CRON_TOKEN=

    # 可选：地点前缀，避免地点信息过于简略
    LOCATION_PREFIX="塘朗科技大专"

    # 可选：过滤关键词列表，包含任意关键词的课程将被过滤掉
    COURSE_NAME_FILTER=["创新实践"]

    # 可选：存储模式，kv / db / dual
    SCHEDULE_STORAGE_MODE=dual

    # 可选：SQLite 路径（db 或 dual 模式生效）
    SCHEDULE_DB_PATH=/app/data/scheduler.db

    # 可选：启用容器启动二维码日志输出（默认 Docker 内启用）
    CAS_QR_BOOTSTRAP_ENABLED=true

    # 可选：二维码刷新周期（秒）
    CAS_QR_BOOTSTRAP_REFRESH_SECONDS=300

    # 可选：二维码状态轮询间隔（秒）
    CAS_QR_BOOTSTRAP_POLL_INTERVAL=3

    # 可选：二维码失败后是否回退账号密码登录
    CAS_QR_ALLOW_PASSWORD_FALLBACK=true

    # 可选：是否默认在 CAS 登录时使用二维码流程
    CAS_USE_QR_LOGIN=false

    # 可选：Blackboard 静态 iCal 回退链接（当 BB JSON 接口异常时启用）
    BB_ICAL_FEED_URL=

    ```

3.  **配置 `Caddyfile`**
    编辑 `Caddyfile` 文件，将 `your-domain.com` 替换为你自己的域名。

    ```
    # Caddyfile

    your-domain.com {
        reverse_proxy ical-service:5001
    }
    ```

4.  **创建运行目录**
  服务需要目录来存放缓存和数据库文件。

    ```bash
  mkdir -p cache data
    ```

5.  **启动服务**
    在项目根目录下，运行以下命令：
    ```bash
    docker-compose up -d --build
    ```
    Docker Compose 将会自动构建镜像、启动服务容器和 Caddy 代理。Caddy 会为你的域名自动申请并配置 SSL 证书。

## 🗓️ 如何使用

服务启动后，你将获得两个独立的日历订阅链接。建议将它们都添加到你的日历应用中。

### Docker 日志扫码登录

首次启动后可直接查看二维码日志并扫码：

```bash
docker logs -f sustech-ical-service
```

你会看到以下关键日志片段：

- `=== CAS QR Bootstrap ===`：表示已生成新的二维码。
- `--- Scan This QR (ASCII) ---`：下方即为可扫码二维码。
- `[qr] status=authorized`：表示扫码授权成功。
- `[qr] CAS token acquired: xxx...yyy`：表示 token 已缓存，可用于后续抓取。

如果二维码过期，服务会自动重新打印新二维码。

#### 1. TIS 课程表日历

此日历包含你的所有课程安排。
`https://<你的域名>/tis/schedule.ics?token=<你在.env文件中设置的ICAL_TOKEN>`

#### 2. Blackboard 作业/DDL 日历

此日历包含所有 Blackboard 课程的作业、测试等事件的截止日期。
`https://<你的域名>/blackboard/schedule.ics?token=<你在.env文件中设置的ICAL_TOKEN>`

**使用示例：**
`https://my-calendar.example.com/tis/schedule.ics?token=a_very_long_and_secret_random_string_12345`
`https://my-calendar.example.com/blackboard/schedule.ics?token=a_very_long_and_secret_random_string_12345`

将这两个链接分别添加到你的日历应用中即可。

## ⚠️ 免责声明 (Disclaimer)

本项目是一个基于网络爬虫的辅助工具，其数据完全依赖于南方科技大学 TIS 教学服务平台和 Blackboard 系统。

由于学校官方网站可能随时进行更新或维护，可能导致本服务的数据抓取失败、延迟或解析错误。因此，通过本服务订阅的日历信息**仅供参考**。

对于任何重要的课程安排或作业截止日期，请务必以 **TIS 和 Blackboard 官网**发布的信息为准。

**对于因使用本服务（包括但不限于数据不及时、不准确或服务中断）而导致的任何后果（如错过课程、作业逾期等），本项目及其开发者概不负责。**

## 📁 项目结构

```
.
├── app.py               # Flask Web 应用核心逻辑
├── tisService.py        # 抓取 TIS 课表的爬虫服务
├── bbService.py         # 抓取 Blackboard 日历的爬虫服务
├── casService.py        # CAS 统一认证服务
├── requirements.txt     # Python 依赖
├── Dockerfile           # 应用的 Docker 镜像构建文件
├── docker-compose.yml   # Docker Compose 部署文件
├── Caddyfile            # Caddy 反向代理和 HTTPS 配置文件
├── .env                 # (本地创建) 存放你的秘密
└── README.md            # 本文档
```

## 📄 许可证

本项目采用 [MIT License](LICENSE) 授权。
