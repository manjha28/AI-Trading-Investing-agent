# Delta lab image: the backend code plus pandas and numpy (the API image does not need them). Build from the repo root.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt && pip install "pandas>=2.2" "numpy>=1.26"
COPY backend/ ./
CMD ["python", "-m", "app.delta_lab.lab_service"]
