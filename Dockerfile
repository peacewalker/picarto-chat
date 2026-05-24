FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir picabot

COPY . /app

RUN mkdir -p /data

CMD ["python", "/app/bot_recorder.py"]
