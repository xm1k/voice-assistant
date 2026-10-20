#!/bin/bash
set -eux

REGISTRY=registry.registry.svc.cluster.local:5000
IMAGE=$REGISTRY/service1:latest

podman build -t $IMAGE .
podman push $IMAGE --tls-verify=false

echo "$KUBECONFIG_DATA" > /tmp/kubeconfig
kubectl --kubeconfig=/tmp/kubeconfig rollout restart deployment service1 -n voice-assistant
