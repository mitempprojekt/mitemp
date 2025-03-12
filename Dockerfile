FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    bluez \
    libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

#Python-Ausgaben direkt an Terminal senden
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "app.py"]