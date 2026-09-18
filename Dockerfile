FROM python:3.11-slim

WORKDIR /app

# PuLP ships with a bundled CBC solver as a wheel on Linux, so no system
# build tools are required.  Keeping the image lean (no build-essential).

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sample ./sample
COPY run.sh ./run.sh
RUN chmod +x ./run.sh

# Default port and host are read from APP_PORT / APP_HOST at runtime.
ENV APP_HOST=0.0.0.0 APP_PORT=8000
EXPOSE 8000

CMD ["./run.sh"]
