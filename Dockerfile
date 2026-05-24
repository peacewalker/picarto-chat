FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot_recorder.py /app/bot_recorder.py

RUN mkdir -p /data

CMD ["python", "/app/bot_recorder.py"]
