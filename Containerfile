FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ffmpeg curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 1001 kryten

COPY . /tmp/kryten-webqueue

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir /tmp/kryten-webqueue

RUN mkdir -p /etc/kryten-webqueue /var/lib/kryten-webqueue /var/log/kryten-webqueue \
    && chown -R kryten:kryten /etc/kryten-webqueue /var/lib/kryten-webqueue /var/log/kryten-webqueue

USER kryten

EXPOSE 2010

ENTRYPOINT ["kryten-webqueue"]
CMD ["--config", "/etc/kryten-webqueue/config.json"]
