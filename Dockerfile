FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY daily_report ./daily_report
COPY social_content ./social_content
COPY locations ./locations

EXPOSE 8080

CMD ["python","main.py"]
