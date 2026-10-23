# Google Calendar

Each user connects their own Google account in Settings → Google Calendar. Голос creates a separate calendar named «Голос». Only meetings are exported, in one direction. Tasks and reminders stay local. Saving a meeting or receiving a successful assistant response confirms the local operation; Google success is shown only by the separate synchronization status.

## Google Cloud setup

Enable the Google Calendar API in a Google Cloud project. Configure the OAuth consent screen and an OAuth web application client. Add your exact public callback URL as an authorized redirect URI: `https://YOUR_HOST/api/v1/integrations/google-calendar/callback`. Set that same URL as `GOOGLE_REDIRECT_URI` in the ConfigMap. For a testing consent screen, add each intended Google account as a test user; observe Google's testing-mode token expiry policy. Production public apps may require Google verification for the requested Calendar scopes.

Provision `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_TOKEN_KEY` in the existing Kubernetes `secrets` Secret through your secret manager. Generate the encryption key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep the key stable across replicas and deployments: changing it makes saved tokens unreadable and requires reconnecting accounts. Never commit credentials, refresh tokens, or encryption keys. Optional Secret references and the blank redirect URI allow installation with the feature disabled.

## Deployment

Apply the additive database migration through the updated auth migration image first, then deploy compatible core, google-calendar, and gateway, in that order. Apply the ConfigMap and provision Secrets before enabling OAuth. The shared build script includes google-calendar; its service build script is also discovered by CI. The service runs its RabbitMQ synchronization consumer from the FastAPI lifespan alongside HTTP on port 8000. Core reaches it through `GOOGLE_CALENDAR_URL=http://google-calendar:8000` and the existing internal token. Build scripts push images and restart deployments; they are not local validation commands. Coordinate Argo CD and CI to preserve the migration order.

The migration only creates new integration tables and retains the existing schema version, allowing rollback of application images without deleting records or integration tables. Stop the Google service before rolling back core; pending outbox work and Google events remain available for a subsequent compatible release.

## Recovery and policy

Connect or reconnect in Settings. OAuth callback outcomes contain only a safe success/error marker. Disconnect stops synchronization; it does not imply that the local meetings were deleted. Reconnect is necessary after access is revoked or token decryption fails.

The interface shows pending, synchronized, failed, conflict, missing and disconnected states. A persistent problem list includes deleted local records, so failed deletions remain visible. WebSocket notifications and reconnects reload authoritative REST state. Technical failures have an explicit retry action. A Google event changed outside Голос becomes a conflict; a remotely deleted event becomes missing. Both permanently block automatic changes for that binding and cannot be cleared with technical retry. Resolve such cases deliberately rather than overwriting or recreating remote data.

Without credentials, Settings explains that the integration is disabled and local calendar behavior continues. Frontend mock mode never calls Google integration endpoints.

Only meetings created while connected acquire a Google binding. Editing an older local meeting does not export it. Disconnect deletes saved credentials and fences pending work without deleting Google events. Reconnecting starts a new connection generation and does not revive old bindings or import their events.

Google supports popup reminders at most 40320 minutes (four weeks) before a meeting. Longer local reminders remain active in Голос; the meeting is exported without a Google popup and its synchronized status explains the limitation. Tasks and standalone reminder records are never exported.

RabbitMQ uses `google_calendar`, `google_calendar.retry`, and `google_calendar.dead` on the default exchange. Quota and transient errors use durable delay queues named `google_calendar.retry.<milliseconds>` so `Retry-After` survives process restarts; unused delay queues expire. After five unsuccessful attempts core records a visible technical error. Retrying uses the same immutable operation ID and Google journal, not a new create request.

Run backend verification with `uv run --project shared --extra dev python -m pytest shared/tests_v2 -q`, and frontend verification with `node --test tests/*.test.js` and `npm run build` from the frontend directory. Google API tests use HTTP transport doubles; real OAuth must be checked with a dedicated test account after Google Cloud configuration. PostgreSQL/RabbitMQ tests require isolated `TEST_POSTGRES_URL` / `TEST_RABBITMQ_URL`, never production services.
