# Сборка из корня репозитория: docker build -f bridges/torugart/Dockerfile -t autovision-bridge:<тег> .
FROM python:3.8

RUN apt-get update && \
    apt-get install -y libglib2.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bridges/torugart/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY dist/ dist/
RUN pip install --no-cache-dir dist/NetSDK-2.0.0.1-py3-none-linux_x86_64.whl

COPY bridges/torugart/main.py bridges/torugart/logger.py ./
COPY bridges/torugart/bridge/ bridge/
COPY bridges/torugart/services/ services/

CMD ["python", "main.py"]
