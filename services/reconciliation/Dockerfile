FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir psycopg2-binary==2.9.11
COPY reconciler.py .
CMD ["python", "reconciler.py"]
