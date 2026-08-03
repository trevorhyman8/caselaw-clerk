"""V5 gate — sends a test digest-shaped message to every allowlisted
Telegram user via the deployed bot, confirming the send path works.
Run: uv run python verify/v5_telegram.py"""
import asyncio
import sys

from telegram import Bot

from pipeline.settings import settings


async def main() -> bool:
    if not settings.telegram_bot_token:
        print("FAIL: TELEGRAM_BOT_TOKEN not set")
        return False
    if not settings.telegram_allowlist:
        print("FAIL: TELEGRAM_ALLOWED_USER_IDS not set")
        return False

    bot = Bot(token=settings.telegram_bot_token)
    ok = True
    for user_id in settings.telegram_allowlist:
        try:
            await bot.send_message(
                chat_id=int(user_id),
                text="✅ caselaw-clerk V5 gate: if you can read this, the bot is wired up correctly. "
                     "Reply anything to confirm the round trip.",
            )
            print(f"sent to {user_id}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL sending to {user_id}: {e}")
            ok = False

    print("PASS — check Telegram and confirm delivery" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
