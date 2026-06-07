FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y ffmpeg --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# spotdl necesita fastapi<0.104, que choca con nuestra versión.
# Lo instalamos en un entorno aislado con pipx para que no interfiera.
RUN pip install --no-cache-dir pipx && \
    pipx install spotdl

ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN mkdir -p downloads

WORKDIR /app/backend

EXPOSE 8000
CMD ["python", "main.py"]
