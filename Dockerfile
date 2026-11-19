FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 resort
COPY --chown=resort:resort backend backend
COPY --chown=resort:resort alembic.ini .
COPY --chown=resort:resort index.html admin.html styles.css admin.css script.js admin.js devantara-resort.jpg file_00000000efec820b99f93b6e6968371f.png ./
COPY --chown=resort:resort googlef17034cc86bbb01d.html ./
USER resort
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
