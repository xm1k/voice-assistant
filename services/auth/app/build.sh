#!/bin/bash
set -eux

echo "ready"
REGISTRY=registry.registry.svc.cluster.local:5000
IMAGE=$REGISTRY/backend:latest
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

podman build -f services/auth/app/Dockerfile -t $IMAGE "$REPO_ROOT"
podman push $IMAGE --tls-verify=false

echo "$KUBECONFIG_DATA" > /tmp/kubeconfig
kubectl --kubeconfig=/tmp/kubeconfig rollout restart deployment auth -n voice-assistant
