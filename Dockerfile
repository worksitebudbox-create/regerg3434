FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY telegram_video_bot/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --create-home --uid 10001 bot

COPY telegram_video_bot/ /app/telegram_video_bot/

USER bot

CMD ["python", "-m", "telegram_video_bot.main"]
