FROM python:3.12-alpine
WORKDIR /app
COPY modellens.py .
COPY config.example.json ./config.example.json
# The real command/args/env are produced by smithery.yaml's commandFunction.
# This CMD is only a fallback for plain `docker run`.
CMD ["python3", "modellens.py", "--once", "ranking"]