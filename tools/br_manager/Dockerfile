FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BR_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home manager \
    && mkdir /data && chown manager:manager /data
COPY app.py .
COPY static ./static
USER manager
EXPOSE 8080
CMD ["python", "app.py"]
