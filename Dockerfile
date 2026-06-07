FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y ffmpeg --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# PATH debe estar definido ANTES de instalar spotdl con pipx
ENV PATH="/root/.local/bin:$PATH"

RUN pip install --no-cache-dir pipx && \
    pipx install spotdl

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN mkdir -p downloads

WORKDIR /app/backend

EXPOSE 8000
CMD ["python", "main.py"]
