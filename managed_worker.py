"""Run one encrypted, registered customer bot as an isolated worker process."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("managed_worker")


async def main(bot_user_id: int, profile: str, token: str) -> None:
    if profile not in {"free", "paid"}:
        raise RuntimeError("Unknown managed bot profile")
    # The master writes this process's token through a private pipe, never argv
    # or the environment. The worker does not receive the master encryption key.
    os.environ["BOT_TOKEN"] = token
    os.environ["BOT_TOKEN_ENV"] = "BOT_TOKEN"
    os.environ["BOT_PROFILE"] = profile
    os.environ["APP_DATA_DIR"] = str(ROOT / "managed_data" / str(bot_user_id))
    if profile == "paid":
        os.environ["XROCKET_ACCESS_PRICE"] = "1"
        os.environ["XROCKET_ACCESS_CURRENCY"] = "TON"

    from bot import main as run_offer_bot

    logger.info("Запускается дочерний бот с ID %s (%s)", bot_user_id, profile)
    await run_offer_bot()


if __name__ == "__main__":
    if len(sys.argv) != 3 or not sys.argv[1].isdigit():
        raise SystemExit("Использование: managed_worker.py BOT_USER_ID PROFILE")
    process_token = sys.stdin.buffer.readline().decode("utf-8").strip()
    if not process_token:
        raise SystemExit("Токен дочернего бота не передан")
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], process_token))
