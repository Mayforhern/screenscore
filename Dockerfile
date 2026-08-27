FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY screenscore/ screenscore/
COPY main.py .
COPY .env.example .env

EXPOSE 8000

ENV PORT=8000

CMD python main.py
