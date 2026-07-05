# ACA sandbox disk image — the microVM the SandboxManager boots per Session.
#
# Unlike the local worker, this image runs NO service of our own: the ACA data
# plane is the exec transport (see §3.1 of the migration doc). It only needs the
# Azure CLI, python3 (azure-identity, for the inception MI token), jq, and the
# FIC bootstrap script. Push this to ACR; SandboxManager turns it into a disk
# image (create_disk_image) and creates sandboxes from it.

FROM mcr.microsoft.com/azure-cli:latest

# azure-cli:latest (Azure Linux / tdnf) already ships python3 + jq; add pip +
# azure-identity so bootstrap.sh can mint the managed-identity token.
RUN tdnf install -y python3-pip jq && tdnf clean all && \
    python3 -m pip install --break-system-packages --no-cache-dir azure-identity

COPY bootstrap.sh /opt/bootstrap.sh
RUN chmod +x /opt/bootstrap.sh

# The platform manages the microVM lifecycle; keep PID 1 alive so the sandbox
# stays up between exec calls.
CMD ["sleep", "infinity"]
