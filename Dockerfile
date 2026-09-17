FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Default: API process (Railway / single-service).
# Same image runs the worker with an alternate CMD / second service:
#   CMD ["python", "-m", "app.worker"]
# See docs/DEPLOY.md for env vars and schema apply order.
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
