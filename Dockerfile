FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Default values; override at runtime with env vars.
ENV GNSS_HOST=0.0.0.0
ENV GNSS_PORT=8080
ENV GNSS_DB_PATH=/data/gnss.db

EXPOSE 8080

CMD ["python", "server.py"]

