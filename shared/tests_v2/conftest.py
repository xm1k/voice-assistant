import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))

import backend  # noqa: E402
import backend.chat  # noqa: E402

for service in ("core", "assistant-worker", "transcription-worker"):
    backend.__path__.append(str(ROOT / "services" / service / "app/backend"))
backend.chat.__path__.append(str(ROOT / "services/core/app/backend/chat"))
