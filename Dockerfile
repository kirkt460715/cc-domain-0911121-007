ARG PRODUCT_RUNTIME=python:3.11.14-slim-bookworm
FROM $PRODUCT_RUNTIME
USER root
WORKDIR /app
COPY . /app
ENTRYPOINT []
CMD ["sh", "demo_support/verify.sh"]
