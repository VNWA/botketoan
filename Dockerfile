FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# `docker compose up` chạy 2 service (bot + tong_ket_bot), cùng image này, khác command — xem docker-compose.yml.
# `docker run …` không compose: mặc định chỉ bot nhóm (override: docker run … python tong_ket_bot.py).
CMD ["python", "bot.py"]
