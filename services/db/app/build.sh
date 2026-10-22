#!/bin/bash
set -eux

echo "ready"
REGISTRY=registry.registry.svc.cluster.local:5000
IMAGE=$REGISTRY/postgres:latest

podman build -f Dockerfile -t $IMAGE .
podman push $IMAGE --tls-verify=false

echo "$KUBECONFIG_DATA" > /tmp/kubeconfig
kubectl --kubeconfig=/tmp/kubeconfig rollout restart deployment db -n voice-assistant
