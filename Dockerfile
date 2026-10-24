FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home appuser && mkdir -p /tmp/uploads && chown appuser /tmp/uploads
USER appuser

ENV STORE=firestore UPLOAD_DIR=/tmp/uploads PORT=8080 TRUST_PROXY=1
CMD exec uvicorn app.main:get_app --factory --host 0.0.0.0 --port ${PORT}
