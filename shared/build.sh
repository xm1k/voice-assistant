#!/bin/bash
set -eux

REGISTRY=registry.registry.svc.cluster.local:5000
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

declare -A IMAGES=(
  [assistant-worker]="assistant-worker"
  [transcription-worker]="speech"
  [core]="search"
  [rag]="rag"
  [auth]="backend"
  [voice]="voice"
  [yandex-calendar]="yandex-calendar"
)

declare -A DEPLOYS=(
  [assistant-worker]="assistant-worker"
  [transcription-worker]="transcription-worker"
  [core]="core"
  [rag]="rag"
  [auth]="auth"
  [voice]="voice"
  [yandex-calendar]="yandex-calendar"
)

for svc in "${!IMAGES[@]}"; do
  podman build \
    -f "$REPO_ROOT/services/$svc/app/Dockerfile" \
    -t "$REGISTRY/${IMAGES[$svc]}:latest" \
    "$REPO_ROOT"
done

for svc in "${!IMAGES[@]}"; do
  podman push "$REGISTRY/${IMAGES[$svc]}:latest" --tls-verify=false
done

echo "$KUBECONFIG_DATA" > /tmp/kubeconfig
for svc in "${!DEPLOYS[@]}"; do
  kubectl --kubeconfig=/tmp/kubeconfig rollout restart deployment "${DEPLOYS[$svc]}" -n voice-assistant
done
