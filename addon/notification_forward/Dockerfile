# Plain Python base image (multi-arch: amd64 + aarch64). The Supervisor's
# default base image has no Python, and build.yaml is deprecated.
FROM python:3.13-slim-bookworm

# The Supervisor passes the app version as a build arg; keep it for the MQTT device info.
ARG BUILD_VERSION
ENV BUILD_VERSION=${BUILD_VERSION}

WORKDIR /opt/notification_forward
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

CMD ["python3", "-m", "app.main"]
