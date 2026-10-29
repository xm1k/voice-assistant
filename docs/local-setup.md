# Локальное развёртывание «Голосок»

Эта инструкция поднимает **весь проект целиком** на локальной машине — по сути это ручное раскатывание продакшн-инфраструктуры на «сервер». Мы не запускаем сервисы по одному в консоли, а разворачиваем их в локальном Kubernetes (k3s), ровно так же, как они живут в проде.

Время первого запуска — 20–40 минут (основная часть уходит на скачивание моделей Whisper и E5).

## Оглавление

1. [Как устроено развёртывание](#как-устроено-развёртывание)
2. [Требования](#требования)
3. [Шаг 1. Поднимаем локальный k3s](#шаг-1-поднимаем-локальный-k3s)
4. [Шаг 2. Настраиваем insecure-реестр для containerd](#шаг-2-настраиваем-insecure-реестр-для-containerd)
5. [Шаг 3. Секреты и ConfigMap](#шаг-3-секреты-и-configmap)
6. [Шаг 4. Поднимаем внутренний registry](#шаг-4-поднимаем-внутренний-registry)
7. [Шаг 5. Собираем и пушим образы](#шаг-5-собираем-и-пушим-образы)
8. [Шаг 6. Применяем манифесты в порядке зависимостей](#шаг-6-применяем-манифесты-в-порядке-зависимостей)
9. [Шаг 7. Проверяем и пользуемся](#шаг-7-проверяем-и-пользуемся)
10. [Структура и порты](#структура-и-порты)
11. [Перезапуск, обновление, откат](#перезапуск-обновление-откат)
12. [Ограничения и диагностика](#ограничения-и-диагностика)

---

## Как устроено развёртывание

Вся конфигурация лежит в git как декларативные Kubernetes-манифесты. Каждый сервис — это `Deployment` (или `Deployment` + `Service`) в `services/<имя>/`. Общие сущности — в `services/base/` (secret, configmap, PVC). Сервисы общаются между собой по DNS-именам кластера (`http://core:8000`, `http://auth:8000`), а наружу выходят через `NodePort`.

Образы собираются с помощью `build.sh` внутри каталога сервиса и помещаются во **внутренний** registry `registry.registry.svc.cluster.local:5000`. Именно этот registry и встроенный DNS-суффикс `registry.svc.cluster.local` — причины, почему локально нужен именно kubernetes, а не docker-compose: деплой-menifestы рассчитаны на кластер.

Три источника конфигурации:

- `services/base/secret.yaml` — пароли и ключи API (base64).
- `services/base/configmap.yaml` — адреса внутренних сервисов и общие настройки.
- `services/<имя>/app/config/providers.yaml` — профили LLM и STT, смонтированные в контейнер.

---

## Требования

- Linux или macOS (на Windows надёжнее всего WSL2).
- [k3s](https://docs.k3s.io/installation) — лёгкий Kubernetes, запустится одной командой. Либо [k3d](https://k3d.io) (k3s в Docker) — см. замечание ниже.
- [kubectl](https://kubernetes.io/docs/tasks/tools/).
- [podman](https://podman.io/) (в продакшене CI собирает именно Podman; образы уже заточены под него) или Docker.
- [uv](https://docs.astral.ru/uv/), Node.js ≥ 20, npm — для локальной разработки/тестов.
- От 20 ГБ свободного диска и от 8 ГБ RAM (модели Whisper + E5 + Ollama).

Замечание про k3d: локально часто удобнее k3d, потому что не требует root и работает в запущенном Docker. Работает та же конфигурация containerd (см. Шаг 2), только флаг для инъекции registries.yaml другой. В примерах ниже за основу взят k3s как «честное продакшн-развёртывание».

---

## Шаг 1. Поднимаем локальный k3s

```sh
# установка (одна строка; уже установлен — пропустите)
curl -sfL https://get.k3s.io | sh -

# kubeconfig кладём в ~/.kube/config
mkdir -p ~/.kube
ln -sf /etc/rancher/k3s/k3s.yaml ~/.kube/config
kubectl get nodes   # должно показать узел в состоянии Ready
```

Узлу с k3s должны быть доступны папка `/dev/net/tun` (не нужна, если не включаете tailscale) и достаточный диск.

---

## Шаг 2. Настраиваем insecure-реестр для containerd

Наш внутренний registry работает по HTTP (без TLS) — `podman push --tls-verify=false` пушит именно так. containerd отказывается тянуть такие образы по умолчанию, поэтому нужно явно разрешить HTTP-реестр. Создаём конфиг:

```sh
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/registries.yaml >/dev/null <<'EOF'
mirrors:
  registry.registry.svc.cluster.local:5000:
    endpoint:
      - "http://registry.registry.svc.cluster.local:5000"
EOF
```

> Если k3s уже запущен — перезапустите его после создания файла:
> `sudo systemctl restart k3s`, иначе конфиг не подхватится.

Для **k3d** конфиг инжектится при создании кластера:

```sh
k3d cluster create voice \
  --k3s-arg '--tls-san=host.k3d.internal' \
  --registry-config /etc/rancher/k3s/registries.yaml
```

---

## Шаг 3. Секреты и ConfigMap

Манифесты используют один Secret `secrets` в namespace `voice-assistant`. В репозитории лежит заготовка `services/base/secret.yaml` с плейсхолдерами — замените значения реальными base64 перед применением.

```sh
# сгенерировать значение: echo -n 'ваше_значение' | base64
```

Ключи, которые ждут деплойменты (полный список):

| Ключ | Назначение | Обязателен? |
|---|---|---|
| `POSTGRES_PASSWORD` | админ-пароль PostgreSQL | да |
| `AUTH_DB_PASSWORD` | пароль схемы `golos_auth` | да |
| `DOMAIN_DB_PASSWORD` | пароль схемы `golos_domain` | да |
| `INTERNAL_TOKEN` | токен для межсервисного доступа | да |
| `RABBITMQ_PASSWORD` | пароль RabbitMQ | да |
| `STT_API_KEY` | ключ внешнего STT (если не локальный Whisper) | по ситуации |
| `OPENROUTER_API_KEY` | ключ OpenRouter (внешняя модель) | для LLM онлайн |
| `DEEPSEEK_API_KEY` | ключ DeepSeek | по ситуации |
| `YANDEX_TOKEN_KEY` | Fernet-ключ для шифрования токена Яндекс Календаря | для календаря |

> В поставляемой заготовке `services/base/secret.yaml` отсутствуют `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `YANDEX_TOKEN_KEY` — если вы хотите внешние модели и календарь, добавьте их в тот же Secret. Без них приложение запустится, но только с локальной Ollama и без синхронизации календаря.

ConfigMap `config` уже заполнен корректными внутренними адресами — его можно применять как есть.

---

## Шаг 4. Поднимаем внутренний registry

Registry живёт в отдельном namespace `registry` (его использует и CI, и билд-скрипты). Создаём namespace и применяем реестр:

```sh
kubectl create namespace voice-assistant || true
kubectl create namespace registry || true

kubectl apply -f infra/registry/deployment.yaml    # registry + registry-pvc (namespace registry)
```

Проверяем, что registry готов (порт 5000 внутри кластера):

```sh
kubectl -n registry get pods
kubectl -n registry wait --for=condition=available deploy/registry --timeout=120s
```

---

## Шаг 5. Собираем и пушим образы

Каждый сервис собирается из своего каталога скриптом `build.sh`, который делает `podman build`, `podman push` во внутренний registry и `rollout restart` деплоймента. Но `build.sh` рассчитан на запуск изнутри кластера (он создаёт kubeconfig из `KUBECONFIG_DATA` и использует `kubectl`). При ручном развёртывании мы делаем те же два шага вручную: собираем образ и пушим его в registry.

Реестр с хоста доступен через NodePort `localhost:32500`, а внутри кластера — по внутреннему имени `registry.registry.svc.cluster.local:5000`. Деплойменты ссылаются на внутреннее имя, но registry хранит образы по имени **repo/тага** (часть после хоста). Поэтому при ручном пуше с хоста важно сохранить ту же часть имени (например `search:latest`), а хост выбираем любой удобный. Определяем переменные:

```sh
export REG_PUB="localhost:32500"   # пуш с хост-машины через NodePort
```

Собираем и пушим пару **hostname=репо-имя** для всех сервисов. Имена образов (repo:tag) и каталоги:

| Сервис | Каталог | Образ (repo:tag) |
|---|---|---|
| db | `services/db/app` | `postgres:latest` |
| auth + миграции | `services/auth/app` | `backend:latest` |
| core | `services/core/app` | `search:latest` |
| rag | `services/rag/app` | `rag:latest` |
| assistant-worker | `services/assistant-worker/app` | `assistant-worker:latest` |
| voice | `services/voice/app` | `voice:latest` |
| transcription-worker | `services/transcription-worker/app` | `speech:latest` |
| yandex-calendar | `services/yandex-calendar/app` | `yandex-calendar:latest` |
| gateway | `services/gateway/app` | `gateway:latest` |

Пример сборки + пуша одного сервиса (из каталога сервиса):

```sh
cd services/core/app
podman build -f Dockerfile -t $REG_PUB/search:latest .            # repo:tag — как в deployment core
podman push $REG_PUB/search:latest --tls-verify=false
```

Для сервисов с многостадийным сбором можно дополнительно собрать слой зависимостей (как делает `build.sh`), чтобы кэш зависимостей не пересобирался каждый раз:

```sh
podman build -f Dockerfile --target dependencies -t $REG_PUB/search:latest-deps .
podman build -f Dockerfile -t $REG_PUB/search:latest .
podman push $REG_PUB/search:latest --tls-verify=false
```

> Критично: **repo-имя после `:`-тага должно совпадать с тем, что указано в deployment**. Есть два адреса одного registry — внутренний (`registry.registry.svc.cluster.local:5000`) и внешний (`localhost:32500`), но `search:latest` в обоих ведёт в одну и ту же репу. Push с узла в кластере или с хоста — неважно, лишь бы repo:tag совпал.
>
> Если собрали без `--tls-verify=false` (HTTPS к HTTP-реестру) — сборка упадёт на пуше; этот флаг обязателен, пока реестр без TLS.

---

## Шаг 6. Применяем манифесты в порядке зависимостей

Порядок важен: сначала инфраструктура, потом сервисы, которые от неё зависят.

**1. Базовые сущности и PVC** (секреты, конфиг, хранилища):

```sh
kubectl apply -f services/base/secret.yaml
kubectl apply -f services/base/configmap.yaml
kubectl apply -f services/base/pvc.yaml              # postgres, rabbitmq, audio, whisper, search
kubectl apply -f services/ollama/pvc.yaml            # ollama-models
```

> Под k3s по умолчанию есть StorageClass `local-path`, PVC заработают без доп. действий. Если хранилища в вашем кластере нет — создайте StorageClass или используйте `--local` провайдер.

**2. Хранилища данных:**

```sh
kubectl apply -f services/db/deployment.yaml -f services/db/service.yaml            # PostgreSQL + pgvector
kubectl apply -f services/rabbitmq/deployment.yaml -f services/rabbitmq/service.yaml # RabbitMQ
kubectl wait --for=condition=available deploy/db --timeout=180s
kubectl wait --for=condition=available deploy/rabbitmq --timeout=180s
```

**3. Остальные сервисы.** Применяем каждый и ждём, пока станет Ready (у многих есть init-контейнер `wait-for-db`):

```sh
for svc in auth core voice yandex-calendar rag assistant-worker transcription-worker gateway ollama webui; do
  kubectl apply -n voice-assistant -f "services/$svc/deployment.yaml"
  [ -f "services/$svc/service.yaml" ] && kubectl apply -n voice-assistant -f "services/$svc/service.yaml"
done
```

(db и rabbitmq уже применены на шаге 2 — повторное применение идемпотентно, так что `kubectl apply` можно без опаски запускать и для них.)

Точный состав манифестов по сервисам посмотрите в [Структуре и портах](#структура-и-порты) — не у каждого есть `service.yaml`.

**4. Миграции БД.** Схемы `golos_auth` и `golos_domain` создаёт init-контейнер `db-init` деплоймента `auth` (он дергает `python -m backend.migrations.main`). Поэтому auth применяется до остальных сервисов, которые ходят в БД. Проверяем, что миграции прошли:

```sh
kubectl -n voice-assistant wait --for=condition=available deploy/auth --timeout=180s
kubectl -n voice-assistant get pods
```

---

## Шаг 7. Проверяем и пользуемся

**1. Ждём готовности всех подов:**

```sh
kubectl -n voice-assistant get pods -w
```

**2. Тестируем «мозг» — готовы ли модели.** Whisper и E5 скачиваются при первом использовании (см. [Ограничения](#ограничения-и-диагностика)). Проверяем логи worker'ов:

```sh
kubectl -n voice-assistant logs deploy/assistant-worker --tail=30
kubectl -n voice-assistant logs deploy/transcription-worker --tail=30
```

**3. Открываем интерфейс.** gateway наружу торчит NodePort: HTTP — `31010`, HTTPS — `31016`. Локально прямо к DNS кластера обращаться нельзя с хоста, поэтому используем NodePort вашего узла:

```sh
# https://<IP_узла>:31016
open https://localhost:31016   # или https://127.0.0.1:31016
```

Сертификат самоподписанный (генерируется в Dockerfile gateway) — браузер попросит подтвердить переход.

Если шлюз по `31016` не открывается, проверьте `kubectl -n voice-assistant get svc gateway` и используйте реальный NodePort.

---

## Структура и порты

| Сервис | Манифест(ы) | Внутренний порт | NodePort (снаружи) | Образ |
|---|---|---|---|---|
| db | `services/db/` | 5432 | — | `postgres:latest` |
| rabbitmq | `services/rabbitmq/` | 5672 / 15672 | — | `rabbitmq:4.1.3-management-alpine` |
| auth | `services/auth/deployment.yaml` | 8000 | 31011 | `backend:latest` |
| core | `services/core/` | 8000 | — | `search:latest` |
| rag | `services/rag/` | 8000 | — | `rag:latest` |
| assistant-worker | `services/assistant-worker/deployment.yaml` | — (worker) | — | `assistant-worker:latest` |
| voice | `services/voice/` | 8000 | — | `voice:latest` |
| transcription-worker | `services/transcription-worker/deployment.yaml` | — (worker) | — | `speech:latest` |
| yandex-calendar | `services/yandex-calendar/` | 8000 | — | `yandex-calendar:latest` |
| gateway | `services/gateway/` | 8080 / 8443 | 31010 / 31016 | `gateway:latest` |
| ollama | `services/ollama/` | 11434 | 31007 | `ollama/ollama` |
| webui | `services/webui/` | 3000 | 31006 | `yidadaa/chatgpt-next-web` |
| registry | `infra/registry/` | 5000 | 32500 | `registry:2` |

---

## Перезапуск, обновление, откат

**Перезапустить конкретный сервис** (новые образ/конфиг подхватятся):

```sh
kubectl -n voice-assistant rollout restart deployment core
```

**Обновить образ.** Пересобрали и запушили новый тег — перезапустим деплоймент:

```sh
cd services/core/app
podman build -f Dockerfile -t $REG_PUB/search:latest .
podman push $REG_PUB/search:latest --tls-verify=false
kubectl -n voice-assistant rollout restart deployment core
```

(`$REG_PUB` из Шага 5, repo:tag `search:latest` должен совпадать с тем, что ожидает deployment core.)

**Откат** к предыдущей ревизии:

```sh
kubectl -n voice-assistant rollout undo deployment core
```

**Поднять из свежего пустого кластера** — достаточно повторить шаги 3–6 (образы уже могут быть в registry, если PVC сохранены).

---

## Ограничения и диагностика

**Модели скачиваются при первом запуске.** Whisper (STT) и E5 (для RAG) качаются и распаковываются в PVC `whisper-models-v2` и `search-models-v2`. Первый запрос голосом и семантический поиск будут заметно медленнее; нужен доступ в интернет с кластера. Если поды restart'ятся на этапе загрузки моделей — подождите, это нормально.

**Внутренний реестр без TLS.** Пуш Pull работает только с `--tls-verify=false` / insecure-настройкой из Шага 2. Без Шага 2 containerd не подтянет образы и поды застрянут в `ImagePullBackOff` — сначала проверьте реестр и конфиг registries.yaml.

**Сертификат gateway самоподписанный.** микрофон работает только по `localhost` или HTTPS — то есть через `https://localhost:31016` или на проде через HTTPS-домен; по «голому» `http://IP:31010` микрофон не заработает.

**Яндекс Календарь.** Для синхронизации нужны `YANDEX_TOKEN_KEY` (Fernet-ключ) и пароль приложения. Без ключа приложение работает полностью, но подключение календаря в настройках недоступно.

**Проверка логов и состояния:**

```sh
kubectl -n voice-assistant logs deploy/assistant-worker --tail=50
kubectl -n voice-assistant describe pod -l app=core
kubectl -n voice-assistant get svc,deploy,pvc
```

**Типовая затычка при диагностике:** под в `CrashLoopBackOff` после применения — смотрим `kubectl -n voice-assistant logs <pod> --previous`, чаще всего это ошибка подключения к БД/RabbitMQ или незаполненный ключ в Secret.