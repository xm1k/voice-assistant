#!/bin/bash
set -eux

REGISTRY=registry.registry.svc.cluster.local:5000
IMAGE=$REGISTRY/service3:latest

podman build -t $IMAGE .
podman push $IMAGE --tls-verify=false

echo "$KUBECONFIG_DATA" > /tmp/kubeconfig
kubectl --kubeconfig=/tmp/kubeconfig rollout restart deployment service3 -n voice-assistant
