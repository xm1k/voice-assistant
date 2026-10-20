#!/bin/bash
# Secrets creation script — run manually on the cluster node

kubectl create namespace tailscale --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic tailscale-auth \
  -n tailscale \
  --from-literal=TS_AUTHKEY=<tailscale-auth-key> \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create namespace woodpecker --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic woodpecker-secret \
  -n woodpecker \
  --from-literal=WOODPECKER_GITHUB_CLIENT=<github-oauth-client-id> \
  --from-literal=WOODPECKER_GITHUB_SECRET=<github-oauth-client-secret> \
  --from-literal=WOODPECKER_AGENT_SECRET=$(openssl rand -hex 32) \
  --dry-run=client -o yaml | kubectl apply -f -

# Generate kubeconfig for voice-assistant namespace (run after ArgoCD syncs voice-assistant-access)
generate_voice_assistant_kubeconfig() {
  TOKEN=$(kubectl get secret voice-assistant-user-token \
    -n voice-assistant \
    -o jsonpath='{.data.token}' | base64 -d)

  SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')

  CA=$(kubectl get secret voice-assistant-user-token \
    -n voice-assistant \
    -o jsonpath='{.data.ca\.crt}')

  cat <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: $CA
    server: $SERVER
  name: voice-assistant
contexts:
- context:
    cluster: voice-assistant
    namespace: voice-assistant
    user: voice-assistant-user
  name: voice-assistant
current-context: voice-assistant
users:
- name: voice-assistant-user
  user:
    token: $TOKEN
EOF
}

# generate_voice_assistant_kubeconfig > voice-assistant-kubeconfig.yaml
