# syntax=docker/dockerfile:1
FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY frontend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app frontend ./frontend
ENV PYTHONUNBUFFERED=1
USER app
EXPOSE 8501
CMD ["streamlit", "run", "frontend/app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
