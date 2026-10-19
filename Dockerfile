FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY countersign ./countersign
COPY samples ./samples
ENV DB_PATH=/data/countersign.db PORT=8000
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn countersign.main:app --host 0.0.0.0 --port ${PORT}"]
