ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /opt/notification_forward
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

CMD ["python3", "-m", "app.main"]
