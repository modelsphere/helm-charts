# The rdma-injector webhook image. It holds the code only -- no certificate.
#
# The Helm chart self-signs the serving certificate at install time, with a SAN
# derived from Release.Namespace, and mounts it from a Secret at /tls. That is
# what lets the chart install into any namespace. See ../templates/webhook.yaml.
#
# Build:
#   cd image && docker build -t 4pdosc/rdma-injector:0.2.0 .
#   docker push 4pdosc/rdma-injector:0.2.0
FROM python:3.12-alpine

COPY inject_webhook.py /app/inject_webhook.py

EXPOSE 8443
ENTRYPOINT ["python3", "/app/inject_webhook.py"]
