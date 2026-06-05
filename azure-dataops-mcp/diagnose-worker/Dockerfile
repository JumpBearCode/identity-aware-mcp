FROM mcr.microsoft.com/azure-cli:latest

# azure-cli:latest is Azure Linux (tdnf) and already ships python3 + jq; add pip.
RUN tdnf install -y python3-pip && tdnf clean all && \
    python3 -m pip install --break-system-packages --no-cache-dir fastapi "uvicorn[standard]" pydantic

WORKDIR /app
COPY worker.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 9001
ENTRYPOINT ["./entrypoint.sh"]
