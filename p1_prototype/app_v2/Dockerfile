# P2 工程版镜像（FastAPI + RAG + 鉴权/缓存/审计/监控）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

# 镜像源可在构建时覆盖，默认用国内镜像（境外网络可传 --build-arg PIP_INDEX_URL=https://pypi.org/simple）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# 构建期代理：部分网络环境下容器直连 pypi 会被干扰（下载停在某个包不动），
# 通过 host.docker.internal 回指宿主机代理可稳定完成安装。
# 用法：docker build --build-arg BUILD_PROXY=http://host.docker.internal:60055 ...
ARG BUILD_PROXY=""
ENV http_proxy=${BUILD_PROXY} \
    https_proxy=${BUILD_PROXY}

WORKDIR /app

# 依赖单独分层，代码变更不触发重新安装
COPY app_v2/requirements.txt /app/app_v2/requirements.txt
RUN pip install --no-cache-dir -r /app/app_v2/requirements.txt

# 业务代码与示例语料
COPY app_v2 /app/app_v2
COPY run_v2.py /app/run_v2.py
COPY run_p2.py /app/run_p2.py
COPY data/docs /app/data/docs

# 非 root 运行
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8012

# 就绪探针由编排层调用 /api/v1/ready（该路径在公开白名单内，无需密钥）
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8012/api/v1/ready', timeout=3).status==200 else 1)"

# --timeout-graceful-shutdown 130：多副本滚动发布时，收到 SIGTERM 后必须等
# 在途请求跑完才能退出。本项目问答会调用 LLM，单次最长可能 120 秒；
# uvicorn 默认值（30s 左右）会把正在生成的回答腰斩，用户看到 502，
# 而日志里那条请求的 access log 是正常的 —— 极难排查。
CMD ["python", "-m", "uvicorn", "app_v2.main:app", "--host", "0.0.0.0", "--port", "8012", "--timeout-graceful-shutdown", "130"]
