"""Load core, assistant-worker and yandex-calendar into one process for contract tests.

Services are deployed independently; these tests only compose their source trees.
`backend.common` modules are per-service copies, so core's (a superset) is loaded.
"""

import sys
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[2] / "services"
sys.path.insert(0, str(SERVICES / "core/app"))

import backend  # noqa: E402
import backend.workers  # noqa: E402

for service in ("assistant-worker", "yandex-calendar"):
    backend.__path__.append(str(SERVICES / service / "app/backend"))
backend.workers.__path__.append(str(SERVICES / "assistant-worker/app/backend/workers"))
