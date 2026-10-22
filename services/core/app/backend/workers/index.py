"""Index worker: consumes RAG indexing jobs from the index queue."""

import asyncio
import logging

from backend.rag.main import handle_index

logger = logging.getLogger(__name__)


async def main():
    logger.info("Index worker starting queue=index")
    from backend.chat.broker import consume

    await consume("index", handle_index)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
