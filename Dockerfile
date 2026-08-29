FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY screenscore/ screenscore/
COPY main.py .

EXPOSE 8000

ENV PORT=8000

CMD ["python", "main.py"]
