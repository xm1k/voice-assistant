#!/bin/bash
set -eux

REGISTRY=registry.registry.svc.cluster.local:5000
IMAGE=$REGISTRY/ci:latest

podman build -t $IMAGE .
podman push $IMAGE --tls-verify=false
