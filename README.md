# Голос v2

Личный помощник для задач, встреч и напоминаний: чат, голосовой ввод, календарь и уведомления. Стек: Python 3.12, FastAPI, PostgreSQL с pgvector, RabbitMQ, React/Vite, nginx и Kubernetes.

- [Архитектура](ARCHITECTURE.md)
- [README v2: данные, запросы пользователя и анимированные схемы](README.v2.md)
- [Контракт модели, команды и ветки обработки](docs/llm-service.md)

## Структура репозитория

- `shared/backend/` — общие схемы, domain, модели БД, брокер, RAG и голосовой ввод; `shared/pyproject.toml` и lockfile — зависимости.
- `services/<имя>/app/` — код конкретного сервиса и Dockerfile. Backend-образы собираются из корня репозитория: сначала общий backend, затем код сервиса поверх него.
- `services/<имя>/` — Deployments, Services и другие ресурсы; `services/base/` — общие ConfigMap, Secret и PVC.
- `services/gateway/app/frontend/` — React-приложение. Оно включается в образ gateway; отдельный `webui` не является frontend Голос v2.
- `apps/` — Argo CD Applications; `infra/` — registry, Woodpecker, RBAC и вспомогательная инфраструктура.

## Развёртывание в Kubernetes

Нужны Kubernetes/k3s, Argo CD, внутренний registry, подходящий storage class и заполненные секреты. Namespace приложения — `voice-assistant`, registry — `registry.registry.svc.cluster.local:5000`.

Перед первым sync:

1. Подготовьте registry и CI по `infra/` и соберите образы.
2. Подготовьте Secret `secrets` в namespace приложения. Нужны пароли PostgreSQL, Auth, domain, RabbitMQ, `INTERNAL_TOKEN`, ключи LLM/STT. Точный перечень — в `secretKeyRef` Deployments. Реальные значения не коммитьте; проверьте содержимое `services/base/secret.yaml` до синхронизации.
3. Проверьте `services/base/configmap.yaml`, PVC, сертификаты gateway и внешний адрес приложения. Пароли в URL соединения должны быть URL-safe.
4. Подключите корневое Application:

```sh
kubectl apply -f apps/app-of-apps.yaml
kubectl -n argocd get applications
kubectl -n voice-assistant get pods,svc,pvc
```

Argo CD читает каталог `apps/`, дочерние Applications — каталоги сервисов. Автоматические prune/self-heal могут отменить ручные изменения управляемых ресурсов. Настройка безопасной доставки секретов должна предшествовать первому sync.

Auth запускает миграцию через init-контейнер. Скрипт образа PostgreSQL создаёт базы, пользователей и pgvector только при первом запуске с пустым томом. На существующем PVC init-скрипты повторно не выполняются; проверяйте отдельно успешность миграции. Обновление приложения не переносит данные из другого проекта автоматически.

### Доступ и HTTPS

Gateway Service имеет NodePort `31010`/`31016`, внутренние порты `8080`/`8443`. nginx требует сертификат и ключ в `/etc/nginx/ssl/`; доставку проверьте в Dockerfile/Deployment gateway. HTTP редиректит на `8443`: для внешнего NodePort нужен согласованный redirect или внешний reverse proxy.

Микрофон работает на HTTPS либо localhost. Cookie сессии HttpOnly, `COOKIE_SECURE` в Auth по умолчанию включён. Текущий `check_origin` не проверяет Origin — приложение не следует считать защищённым такой проверкой только по наличию функции.

## Модели и конфигурация

ConfigMap `config` задаёт `AUTH_URL`, `CORE_URL`, `RAG_URL`, `AUDIO_DIR` и `PROVIDERS_CONFIG`. Пароли и ключи приходят из Secret `secrets`.

Активные LLM-профили описаны в `services/assistant-worker/app/config/providers.yaml`: адрес, модель, имя env-переменной ключа, timeout, лимит токенов и дополнительные заголовки. Этот файл встроен в образ worker. ConfigMap `providers` сейчас не смонтирован: изменение только его содержимого не меняет настройки worker.

Worker регистрирует названия профилей в core. Выбор провайдера хранится в БД и доступен в настройках интерфейса; worker кеширует выбор до 30 секунд. При недоступности настройки используется кеш либо первый профиль. Не ожидайте мгновенного переключения уже выполняющегося запроса.

Провайдер должен поддерживать JSON Schema; HTTP 200 и `strict: true` не заменяют проверку ответа. Ошибки валидации возвращаются модели для исправления. После восьми обращений задание завершается понятной ошибкой. Отказ HTTP 402 требует проверки баланса/лимита ключа, а не перезапуска pod.

Whisper и E5 загружают модели при использовании. Учитывайте память, диск, доступ к загрузке и время первого запроса. Ключи находятся в env, а не в YAML.

## Локальная разработка

Backend использует зависимости `shared/pyproject.toml`:

```sh
cd shared
uv sync --extra dev
```

Для запуска конкретного сервиса собирайте runtime так же, как Dockerfile: `shared/backend/` плюс overlay `services/<имя>/app/backend/`. Одного добавления этих каталогов в PYTHONPATH недостаточно для всех подпакетов. Потребуются отдельные PostgreSQL, RabbitMQ и переменные адресов зависимостей. Compose-файлов в этом репозитории нет.

Frontend без backend:

```sh
cd services/gateway/app/frontend
npm ci
npm run dev:mock
```

Обычный `npm run dev` использует Vite proxy на `localhost:8001`; локальный доступ к gateway настройте отдельно. `npm run build` собирает интерфейс; существующие проверки запускаются `node --test tests/*.test.js`. Для локальной диагностики используйте подменённые провайдеры без платных вызовов и изменения production-данных.

## Сборка, обновление и эксплуатация

Woodpecker запускает `infra/ci/run_cd.py` для изменённых каталогов и затем sync Argo CD. `shared/build.sh` пересобирает все backend-образы. Скрипты сборки используют Podman, push в registry и rollout restart: это не команды только для локальной проверки. Pipeline требует `KUBECONFIG_DATA` и `ARGOCD_TOKEN`.

Build-runner выводит ошибки отдельных сборок, но не гарантирует ненулевой итоговый код; проверяйте его логи и rollout. Для нового endpoint валидации сначала обновите core, затем assistant-worker и gateway. Автоматическая сборка не гарантирует такой порядок. Тег `latest` изменяемый: для надёжного отката сохраняйте проверенный digest образа/ревизию.

```sh
kubectl -n voice-assistant get pods -o wide
kubectl -n voice-assistant get events --sort-by=.lastTimestamp
kubectl -n voice-assistant logs deployment/core -c core --tail=100 --timestamps
kubectl -n voice-assistant logs deployment/assistant-worker -c assistant-worker --tail=100 --timestamps
kubectl -n voice-assistant rollout status deployment/core
```

Успех определяется статусом задания и результатом core, а не HTTP 200 от модели. Frontend показывает ошибки форм рядом с действием, фоновые — в закрываемой области; текст сохраняется при отказе отправки.

Не удаляйте PVC БД и брокера при обычном обновлении. Делайте отдельные backup `golos_auth` и `golos_domain`, проверяйте восстановление. Общие audio/search PVC используют ReadWriteOnce: размещение на нескольких узлах требует соответствующего хранилища или согласованного размещения pod. Одиночные PostgreSQL/RabbitMQ не дают высокой доступности. Мониторинг, backup и ротация сертификатов требуют отдельной эксплуатации.

Настройка интеграции: [Яндекс Календарь](docs/yandex-calendar.md).
