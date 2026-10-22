# syntax=docker/dockerfile:1
FROM node:24.10.0-alpine3.22 AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginx:1.28.0-alpine AS runtime
WORKDIR /usr/share/nginx/html
RUN apk add --no-cache openssl \
    && mkdir -p /tmp/nginx /etc/nginx/ssl \
    && openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
         -keyout /etc/nginx/ssl/key.pem \
         -out /etc/nginx/ssl/cert.pem \
         -subj "/CN=voice-assistant" \
    && chown -R nginx:nginx /tmp/nginx /etc/nginx/ssl
COPY --link docker/nginx.conf /etc/nginx/nginx.conf
COPY --link docker/proxy_params /etc/nginx/proxy_params
COPY --link --from=frontend /src/dist /usr/share/nginx/html
USER nginx
EXPOSE 8080 8443
CMD ["nginx", "-g", "daemon off;"]
