FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
   docker-compose.yml:
services:
  bot:
    build: .
    environment:
      - BOT_TOKEN=${BOT_TOKEN}
    restart: unless-stopped