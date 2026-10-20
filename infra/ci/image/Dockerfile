FROM quay.io/podman/stable

RUN dnf install -y python3 git --quiet && \
    curl -LO "https://dl.k8s.io/release/v1.31.0/bin/linux/amd64/kubectl" && \
    chmod +x kubectl && \
    mv kubectl /usr/local/bin/ && \
    dnf clean all
