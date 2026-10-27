#!/bin/bash
set -eux

echo "ready"
REGISTRY=registry.registry.svc.cluster.local:5000
IMAGE=$REGISTRY/assistant-worker:latest

# Tagged so CI image pruning keeps the dependency layers cached.
podman build -f Dockerfile --target dependencies -t $IMAGE-deps .
podman build -f Dockerfile -t $IMAGE .
podman push $IMAGE --tls-verify=false

echo "$KUBECONFIG_DATA" > /tmp/kubeconfig
kubectl --kubeconfig=/tmp/kubeconfig rollout restart deployment assistant-worker -n voice-assistant
