FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD sh -c "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"
