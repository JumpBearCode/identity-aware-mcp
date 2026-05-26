FROM mcr.microsoft.com/azure-cli:latest

RUN apk add --no-cache python3 py3-pip jq && \
    pip install --break-system-packages --no-cache-dir fastapi uvicorn[standard] pydantic

WORKDIR /app
COPY worker.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 9002
ENTRYPOINT ["./entrypoint.sh"]
