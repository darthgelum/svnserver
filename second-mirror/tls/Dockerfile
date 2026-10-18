FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends nginx ca-certificates openssl tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir certbot==5.8.0 \
    && rm -f /etc/nginx/sites-enabled/default
COPY run.py /app/run.py
ENV PYTHONUNBUFFERED=1
EXPOSE 80 443
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/run.py"]
CMD ["serve"]
