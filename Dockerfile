FROM python:3.10-alpine AS builder

WORKDIR /app

# 只安装构建所需的依赖
RUN apk add --no-cache --virtual .build-deps \
    gcc \
    musl-dev \
    libffi-dev \
    build-base

# 创建虚拟环境并安装依赖
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 第二阶段：最终镜像
FROM python:3.10-alpine

# 添加元数据标签
LABEL maintainer="coderxiu<coderxiu@qq.com>"
LABEL description="闲鱼AI客服机器人"
LABEL version="2.1"

# 设置时区和编码
ENV TZ=Asia/Shanghai \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 只安装运行时必要的包
RUN apk add --no-cache \
    tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    # 清理apk缓存
    && rm -rf /var/cache/apk/*

# 设置工作目录
WORKDIR /app

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv

# 控制台监听容器内所有网卡（宿主机端口映射只绑定 127.0.0.1）；
# .env 放进 data 目录，Cookie 和控制台配置随 data 卷一起保留
ENV WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=8765 \
    ENV_FILE=/app/data/.env

# 创建必要的目录
RUN mkdir -p data

# 提示词模板（自定义提示词由控制台保存到挂载的 prompts 目录）
COPY prompts/ prompts/

# 只复制运行所需的文件
COPY main.py XianyuAgent.py XianyuApis.py context_manager.py webui.py .env.example ./
COPY utils/ utils/
COPY console/ console/
COPY webui/ webui/

EXPOSE 8765

# 启动网页控制台，机器人由控制台负责启动和重启
CMD ["python", "webui.py"]
