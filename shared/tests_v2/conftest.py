import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))

import backend  # noqa: E402
import backend.chat  # noqa: E402
import backend.workers  # noqa: E402
import backend.yandex_calendar  # noqa: E402

for service in ("core", "assistant-worker", "transcription-worker"):
    backend.__path__.append(str(ROOT / "services" / service / "app/backend"))
backend.chat.__path__.append(str(ROOT / "services/core/app/backend/chat"))
backend.workers.__path__.append(str(ROOT / "services/assistant-worker/app/backend/workers"))
backend.yandex_calendar.__path__.append(str(ROOT / "services/yandex-calendar/app/backend/yandex_calendar"))
