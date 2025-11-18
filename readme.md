# 南方科技大学课表 iCal 订阅服务

这是一个自托管服务，用于抓取你的南科大 TIS 教学服务平台上的课程表，并将其转换为一个安全、可自动更新的 iCalendar (.ics) 订阅链接。你可以将此链接添加到任何支持 iCal 订阅的日历应用中（如 Apple Calendar, Google Calendar, Outlook 等）。

## ✨ 功能特性

- **标准 iCal 订阅**: 提供与绝大多数日历应用兼容的 `.ics` 订阅链接。
- **自动更新与缓存**: 服务会自动缓存课表，并每周更新一次，确保你的日历保持最新，支持节假日调休或停课（例如运动会），同时最大限度地减少对学校服务器的请求。
- **安全与私密**:
    - **Token 鉴权**: 订阅链接包含一个私有令牌，防止他人未经授权访问你的课表。
    - **自托管**: 你的学号和密码仅存在于你自己的服务器上，不会泄露给任何第三方。
    - **自动 HTTPS**: 通过 Caddy 自动配置 HTTPS，确保数据传输全程加密。
- **轻松部署**: 使用 Docker 和 Docker Compose 完全容器化，一条命令即可完成部署。
- **保留历史记录**: 在日历中保留过去一个月的课程记录，方便回顾。

## 🚀 部署指南

部署此服务需要你有一台拥有公网 IP 的服务器，并为其准备一个域名。

### 准备工作

1.  一台拥有公网 IP 的服务器（例如，任何云服务商的 VPS）。
2.  一个域名，并将其 DNS A/AAAA 记录指向你服务器的公网 IP。
3.  在服务器上安装 [Docker](https://docs.docker.com/engine/install/) 和 [Docker Compose](https://docs.docker.com/compose/install/)。

### 安装步骤

1.  **克隆项目代码**
    ```bash
    git clone https://github.com/whiteki08/SUSTechReptile.git
    cd SUSTechReptile
    ```

2.  **创建并配置 `.env` 文件**
    此文件用于存放你的敏感信息。
    ```bash
    # 复制模板文件
    cp .env.example .env
    ```
    然后，编辑 `.env` 文件，填入你的个人信息：
    ```ini
    # .env
    
    # 你的南科大学号
    SUSTECH_SID=12345678
    
    # 你的南科大 CAS 登录密码
    SUSTECH_PASSWORD=your_secret_password
    
    # 用于订阅链接的安全令牌，请设置为一个长且随机的字符串,可以使用`openssl rand -base64 64`生成
    ICAL_TOKEN=
    ```

3.  **配置 `Caddyfile`**
    编辑 `Caddyfile` 文件，将 `your-domain.com` 替换为你自己的域名。
    ```
    # Caddyfile
    
    your-domain.com {
        reverse_proxy ical-service:5001
    }
    ```

4.  **创建缓存目录**
    服务需要一个目录来存放生成的日历缓存文件。
    ```bash
    mkdir cache
    ```

5.  **启动服务**
    在项目根目录下，运行以下命令：
    ```bash
    docker-compose up -d
    ```
    Docker Compose 将会自动构建镜像、启动服务容器和 Caddy 代理。Caddy 会为你的域名自动申请并配置 SSL 证书。

## 🗓️ 如何使用

服务启动后，你的日历订阅链接格式如下：

`https://<你的域名>/schedule.ics?token=<你在.env文件中设置的ICAL_TOKEN>`

例如：
`https://my-calendar.example.com/schedule.ics?token=a_very_long_and_secret_random_string_12345`

将此链接添加到你的日历应用中即可：
- **Apple Calendar**: 文件 -> 新建日历订阅 -> 粘贴链接。
- **Google Calendar**: 其他日历 -> 添加 -> 通过网址添加 -> 粘贴链接。
- **Outlook**: 添加日历 -> 从 Web 订阅 -> 粘贴链接。

## 📁 项目结构

```
.
├── app.py               # Flask Web 应用核心逻辑
├── tisService.py        # 抓取 TIS 课表的爬虫服务
├── requirements.txt     # Python 依赖
├── Dockerfile           # 应用的 Docker 镜像构建文件
├── docker-compose.yml   # Docker Compose 部署文件
├── Caddyfile            # Caddy 反向代理和 HTTPS 配置文件
├── .env.example         # 环境变量模板文件
├── .gitignore           # Git 忽略文件配置
└── README.md            # 本文档
```

## 📄 许可证

本项目采用 [MIT License](LICENSE) 授权。
