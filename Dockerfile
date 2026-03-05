FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py md_to_html.py telegram_send.py agent_client.py media.py handlers.py bot.py ./

CMD ["python", "bot.py"]
