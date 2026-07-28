FROM docker.1ms.run/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY asgi.py .
COPY mcp_server.py .
COPY static ./static

RUN mkdir -p /data

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)"

CMD ["uvicorn", "asgi:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--ws-ping-interval", "60", "--ws-ping-timeout", "45"]
