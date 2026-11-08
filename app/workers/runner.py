import asyncio
import logging

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Job
from app.db.session import get_session_factory, init_models

log = logging.getLogger("worker")


async def queued_job_count() -> int:
    factory = get_session_factory()
    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(Job).where(Job.status == "queued")
        )
    return int(count or 0)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if get_settings().init_db:
        await init_models()
    while True:
        waiting = await queued_job_count()
        log.info("queued jobs: %s", waiting)
        await asyncio.sleep(5)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
