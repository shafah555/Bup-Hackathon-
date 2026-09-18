FROM python:3.11-slim

WORKDIR /app

# Install OS deps for PuLP's CBC solver.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY sample ./sample
COPY run.sh ./run.sh
RUN chmod +x ./run.sh

# Default port and host are read from APP_PORT / APP_HOST at runtime.
ENV APP_HOST=0.0.0.0 APP_PORT=8000
EXPOSE 8000

CMD ["./run.sh"]
