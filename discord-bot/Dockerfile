FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# docker and docker-compose binaries are bind-mounted from the host at runtime
# (/usr/bin/docker and /usr/libexec/docker/cli-plugins/docker-compose)

CMD ["python", "-u", "bot.py"]
