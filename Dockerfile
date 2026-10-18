FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHATBOX_HOST=0.0.0.0 \
    CHATBOX_PORT=7860 \
    CHATBOX_DEMO_MODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 1000 appuser

COPY --chown=appuser:appuser chatbox_app.py .
COPY --chown=appuser:appuser chatbox ./chatbox

USER appuser
EXPOSE 7860

CMD ["python", "chatbox_app.py"]
