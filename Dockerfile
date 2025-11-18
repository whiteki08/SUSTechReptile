# 1. 使用一个官方的、轻量级的 Python 镜像作为基础
FROM python:3.10-slim

# 2. 设置工作目录，容器内所有操作都将在这个目录下进行
WORKDIR /app

# 3. 复制依赖文件到工作目录
#    我们先复制这个文件并安装依赖，这样可以利用 Docker 的缓存机制。
#    只要 requirements.txt 不变，就不需要重新安装依赖。
COPY requirements.txt .

# 4. 安装项目依赖，--no-cache-dir 可以减小镜像体积
RUN pip install --no-cache-dir -r requirements.txt

# 5. 复制项目的所有源代码到工作目录
COPY . .

# 6. 声明容器将要监听的端口（与 app.py 中设置的端口一致）
EXPOSE 5001

# 7. 设置容器启动时要执行的命令
CMD ["python", "app.py"]