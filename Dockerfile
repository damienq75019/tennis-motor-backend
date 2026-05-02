FROM python:3.13-slim

WORKDIR /app

COPY . /app

RUN python -m pip install --upgrade pip
RUN python -m pip install fastapi uvicorn pydantic requests beautifulsoup4

ENV PORT=8080

CMD ["python", "app.py"]
