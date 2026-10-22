# Архитектура Голос v2

[Запуск и эксплуатация](README.md) · [Команды и ветки модели](docs/llm-service.md)

## Компоненты

Приложение работает в namespace `voice-assistant`. Каждый сервис имеет Deployment; HTTP-компоненты доступны через Service/DNS. Общий код находится в `shared/backend/`, код конкретного сервиса накладывается на него при сборке. Ранее дублировавшиеся схемы теперь имеют один источник в shared.

```mermaid
flowchart TB
    browser["Браузер · React"] <-->|"HTTPS / WebSocket"| gateway["gateway · nginx"]
    gateway --> auth["auth:8000"]
    gateway --> voice["voice:8000"]
    gateway <--> core["core:8000 · API и фоновые процессы"]
    auth --> authdb[("PostgreSQL · golos_auth")]
    core --> domain[("PostgreSQL + pgvector · golos_domain")]
    core -.-> auth
    voice -.-> auth
    voice --> audio[("PVC audio")]
    voice -->|"job"| core
    core -->|"outbox"| rabbit["rabbitmq:5672"]
    rabbit -->|"chat"| worker["assistant-worker"]
    worker <-->|"claim / validate / retry"| core
    worker <-->|"JSON Schema"| llm["Выбранный LLM-провайдер"]
    worker <-->|"query"| rag["rag:8000"]
    rag --> core
    rag --> domain
    worker -->|"llm_response"| rabbit
    rabbit -->|"results"| core
    rabbit -->|"transcription"| transcription["transcription-worker · Whisper / STT"]
    audio --> transcription
    transcription -->|"текст / результат"| core
```

| Deployment | Точка запуска | Ответственность |
|---|---|---|
| gateway | nginx | Статика React, HTTPS, HTTP/WebSocket proxy |
| auth | `backend.auth.main:app` | Пользователи, пароли, сессии и timezone |
| core | `backend.chat.application` | Записи, чат, jobs, события и применение решений |
| voice | `backend.voice.main:app` | Авторизация загрузки аудио и регистрация job |
| rag | `backend.rag.main:app` | Типизированные запросы и гибридный поиск |
| assistant-worker | `backend.workers.assistant` | Контекст, LLM, исправления и публикация решения |
| transcription-worker | `backend.workers.transcription` | Распознавание и передача текста в core |
| db / rabbitmq / ollama | Соответствующие образы | Хранение, доставка, локальный inference |

Core запускает пять дочерних процессов: API, publisher, results, reminders и index. Это один контейнер. Остановка дочернего процесса завершает группу; Kubernetes перезапускает контейнер. SIGTERM передаётся всем процессам, после 20 секунд оставшиеся завершаются принудительно.

## Данные и границы доверия

Auth и domain используют разные базы/пользователей PostgreSQL на общем сервере. Auth хранит хеш сессионного токена; браузер отправляет cookie `golos_session`. Core и Voice проверяют сессию через Auth. Внутренний HTTP защищён `X-Internal-Token`; gateway закрывает `/internal`. Внешние порты приложения публикует gateway.

Модель не обращается к БД и не выполняет SQL. Владелец бизнес-правил — core/shared domain: права на запись, версия, параметры, generation диалога и claim token. Подтверждение мутаций core формирует из сохранённых значений. `ready` с одним текстом допустим для обычного ответа и ничего не изменяет.

## Жизненный цикл задания

1. Core транзакционно сохраняет сообщение, job и outbox до HTTP 202. Publisher передаёт идентификатор job в RabbitMQ.
2. Worker получает через claim историю, pending-контекст, timezone и время запроса. Используются последние 30 релевантных сообщений.
3. Модель возвращает `clarify`, `query` или `ready`. Worker проверяет схему и ссылки, выполняет query через RAG.
4. Для конечного решения вызывается `/internal/jobs/{job_id}/validate`. Core использует тот же код применения, что и сохранение, внутри обязательно откатываемого savepoint. Записи, версии, outbox и события не сохраняются. Ошибки возвращаются модели; общий предел — восемь обращений.
5. Worker публикует проверенное решение в `llm_response`. Процесс results передаёт его core, который повторно проверяет данные и применяет весь пакет атомарно. Предварительная проверка не резервирует состояние: последующий конфликт версии остаётся возможным.
6. Ответ и события сохраняются в БД и поступают в браузер. Повтор терминального результата не применяет команды снова. Ошибка финального применения сохраняется как понятное сообщение об ошибке.

Lease задания — 12 минут; очистка чата увеличивает generation и отменяет применение старого ответа. Есть retry/dead очереди и лимит пяти попыток доставки. Долгие модельные вызовы и порядок обновления сервисов должны укладываться в эти ограничения; подробности ACK и восстановления смотрите в `shared/backend/chat/broker.py`.

Голос отправляется одним файлом. После STT backend создаёт текстовое сообщение и assistant-job; frontend не отправляет распознанный текст повторно. Очистка истории отменяет применение старой транскрипции. HTTP восстанавливает состояние после переподключения WebSocket.

## Хранение, конфигурация и ошибки

PVC обеспечивают PostgreSQL, RabbitMQ, аудио и кеши моделей. Audio разделяют Voice и transcription-worker; search-кеш — core и RAG. ReadWriteOnce требует согласованного размещения на узлах и не является готовой отказоустойчивой схемой.

Адреса и путь конфигурации приходят из ConfigMap `config`, ключи — из Secret `secrets`. Файл профилей встроен в образ assistant-worker; ConfigMap `providers` сейчас не смонтирован. Выбор профиля хранится в Setting в domain-БД, регистрируется worker и кешируется им до 30 секунд. Подробнее — README.

Ошибки схемы и бизнес-правил возвращаются модели как структурированные данные с путями полей. Ошибки сети обрабатываются механизмом доставки. Пользователь получает русские сообщения без traceback, ключей и сырых JSON. Интерфейс различает ошибки форм, фоновых операций и заданий, не дублируя уже сохранённые сообщения чата.
