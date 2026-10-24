"""Запуск отдельного платного экземпляра бота."""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv(override=True)
os.environ["BOT_PROFILE"] = "paid"

from bot import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
