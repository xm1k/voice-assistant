"""Index worker: consumes RAG indexing jobs from the index queue."""

import asyncio
import logging

from backend.rag.main import handle_index

logger = logging.getLogger(__name__)


async def consume_forever(queue, handler):
    from backend.chat.broker import consume
    while True:
        try:
            await consume(queue, handler)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Consumer exited queue=%s, reconnecting in 5s", queue)
            await asyncio.sleep(5)


async def main():
    logger.info("Index worker starting queue=index")
    await consume_forever("index", handle_index)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
