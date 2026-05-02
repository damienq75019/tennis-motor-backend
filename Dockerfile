FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY . /app

RUN python -m pip install --upgrade pip
RUN python -m pip install fastapi uvicorn pydantic requests beautifulsoup4

ENV PORT=8080

CMD ["python", "app.py"]
