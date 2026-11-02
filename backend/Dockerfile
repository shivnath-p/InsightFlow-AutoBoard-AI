FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 appuser \
    && mkdir -p /app/data && chown -R appuser /app/data
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health', timeout=3)"
# Behind a reverse proxy add: --proxy-headers --forwarded-allow-ips <proxy ip>
# (needed so rate limits see the real client IP; never trust the header from the open internet)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
