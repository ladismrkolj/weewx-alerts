#!/usr/bin/env python3
"""
get_chat_id.py - find the chat_id for a Telegram bot token.

UserAlerts needs both a bot_token AND a chat_id to send you messages.
Telegram bots can't discover the chat_id on their own -- a bot is only
allowed to see chats where someone has messaged it first. This script reads
that history back via the getUpdates API call.

Usage:
    1. Create a bot via @BotFather in Telegram and get its token
       (see weewx-alerts/README.md for the step-by-step).
    2. Open a chat with your new bot in Telegram and send it ANY message,
       e.g. "hi". (Bots can't message you first -- Telegram requires you to
       message them, once, before they're allowed to message you back.)
    3. Run this script with that token:

           python3 tools/get_chat_id.py <BOT_TOKEN>

       It prints the chat_id of everyone who has messaged the bot recently.

No weewx / venv dependency -- plain stdlib, runs with any python3.
"""
import json
import sys
import urllib.error
import urllib.request


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 get_chat_id.py <BOT_TOKEN>")
    bot_token = sys.argv[1]

    url = "https://api.telegram.org/bot%s/getUpdates" % bot_token
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')
        sys.exit("Telegram API rejected the request (HTTP %s): %s\n"
                  "Double check the bot token." % (e.code, detail))
    except Exception as e:
        sys.exit("Request to Telegram failed: %s" % e)

    if not body.get("ok"):
        sys.exit("Telegram API error: %s" % body)

    results = body.get("result", [])
    if not results:
        sys.exit(
            "No messages found for this bot yet.\n"
            "Open a chat with your bot in Telegram, send it any message, "
            "then run this script again."
        )

    seen = {}
    for update in results:
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg["chat"]
        seen[chat["id"]] = chat

    if not seen:
        sys.exit("Updates were returned but none contained a chat message. "
                  "Try sending the bot a plain text message and re-run.")

    print("Found %d chat(s) that have messaged this bot:" % len(seen))
    for chat_id, chat in seen.items():
        label = (chat.get("username") or chat.get("title")
                  or chat.get("first_name") or "")
        print("  chat_id = %s   (%s, type=%s)"
              % (chat_id, label, chat.get("type")))
    print("\nUse one of the chat_id values above in your user's "
          "channels.telegram.chat_id config.")


if __name__ == "__main__":
    main()
