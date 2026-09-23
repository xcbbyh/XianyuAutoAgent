# 用 Debian slim：依赖都有现成的 manylinux 轮子（playwright 没有 Alpine/musl 版本），不需要编译
FROM python:3.10-slim

# 添加元数据标签
LABEL maintainer="coderxiu<coderxiu@qq.com>"
LABEL description="闲鱼AI客服机器人"
LABEL version="2.1"

# 设置时区和编码
ENV TZ=Asia/Shanghai \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone

# 设置工作目录
WORKDIR /app

# 先装依赖，改代码后重建可以复用这一层
COPY requirements.txt .
RUN pip install -r requirements.txt

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
