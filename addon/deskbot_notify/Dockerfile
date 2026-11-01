ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /opt/deskbot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

CMD ["python3", "-m", "app.main"]
