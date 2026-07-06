"""Find your Telegram chat ID for a given bot token.

Deal Finder needs a bot token (create one via @BotFather on Telegram, no cost) and, per
watch, the numeric chat ID to send that watch's notifications to. There's no way to look
up a chat ID except by having the user message the bot first — Telegram doesn't expose a
reverse lookup by username. So:

  1. Message your bot on Telegram (anything, e.g. "hi") — or add it to a group and send a
     message there.
  2. Run this script. It calls the bot API's getUpdates and prints every chat it has seen
     a message from, with the sender's name, so you can identify and copy the right one.
  3. Paste that chat ID into Settings -> Telegram (as the default) or into a specific
     watch's Telegram chat ID field.

Run:  python -m deal_finder.notify.telegram_setup [--token TOKEN]
      (defaults to the token already saved in Settings/DF_TELEGRAM_BOT_TOKEN if omitted)
"""

from __future__ import annotations

import argparse
import sys

import httpx

from .telegram import TELEGRAM_API


def _fetch_updates(token: str) -> list[dict]:
    resp = httpx.get(f"{TELEGRAM_API}/bot{token}/getUpdates", timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise SystemExit(f"Telegram API error: {data.get('description') or resp.status_code}")
    return data.get("result", [])


def _describe_chat(chat: dict) -> str:
    name = chat.get("title") or " ".join(
        p for p in (chat.get("first_name"), chat.get("last_name")) if p
    ) or chat.get("username") or "(unnamed)"
    return f"{chat['id']}\t{chat.get('type', '?'):10s}\t{name}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", help="Bot token; defaults to the saved Telegram bot token in Settings.")
    args = parser.parse_args(argv)

    token = args.token
    if not token:
        from sqlmodel import Session

        from ..db import get_engine, runtime_settings

        with Session(get_engine()) as session:
            token = runtime_settings(session).telegram_bot_token
    if not token:
        print("No bot token given (--token) and none configured (Settings -> Telegram).")
        sys.exit(1)

    updates = _fetch_updates(token)
    if not updates:
        print(
            "No messages found for this bot yet. Message your bot on Telegram first "
            "(anything, e.g. 'hi'), then run this again."
        )
        return

    seen: dict[int, str] = {}
    for update in updates:
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat")
        if chat and chat.get("id") not in seen:
            seen[chat["id"]] = _describe_chat(chat)

    print("chat_id\ttype\t\tname")
    for line in seen.values():
        print(line)
    print("\nCopy the chat_id for yourself into Settings -> Telegram, or a watch's Telegram chat ID field.")


if __name__ == "__main__":
    main()
