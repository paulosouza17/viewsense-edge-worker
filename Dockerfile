FROM python:3.11-slim

# System deps for OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download YOLO model
COPY yolov8n.pt .

# App code
COPY *.py *.yaml ./

EXPOSE 8765

CMD ["python", "main.py"]
