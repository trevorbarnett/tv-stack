FROM python:3.12-alpine

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY resolve.py .
COPY crontab /etc/crontabs/root

CMD ["crond", "-f", "-l", "2"]
