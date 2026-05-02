FROM python:3.13-slim

WORKDIR /app

COPY . /app

RUN python -m pip install --upgrade pip
RUN python -m pip install requests beautifulsoup4 playwright
RUN python -m playwright install --with-deps chromium

ENV PORT=8080

CMD ["python", "app.py"]
