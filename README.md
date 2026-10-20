# TikTok Video Monitor

This repository contains a Telegram bot that monitors public TikTok profiles
and sends administrators previews and links when new videos appear. Profile
and video history is stored in Supabase.

## Run the bot locally

1. Create `telegram_video_bot/.env` from
   [`telegram_video_bot/.env.example`](telegram_video_bot/.env.example).
2. Set `BOT_TOKEN`, `ADMIN_IDS`, `SUPABASE_URL`, and
   `SUPABASE_SERVICE_ROLE_KEY` in that local file. Do not commit or share
   credentials.
3. Run the SQL in [`telegram_video_bot/supabase_schema.sql`](telegram_video_bot/supabase_schema.sql)
   in the Supabase SQL Editor.
4. Install and start the bot from the repository root:

   ```powershell
   python -m pip install -r telegram_video_bot/requirements.txt
   python -m telegram_video_bot.main
   ```

The bot uses Telegram long polling and checks tracked profiles every 15 minutes
by default. Admins must send `/start` before the bot can message them.

## Deploy the bot to Railway

In Railway, create a project from this GitHub repository and deploy a service
using the root [`Dockerfile`](Dockerfile). The
[`railway.json`](railway.json) file selects Docker and starts the bot with
Telegram long polling.

Add these variables in the Railway service's **Variables** settings:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Optional: configure `POLL_INTERVAL_SECONDS` and `MAX_VIDEOS_PER_PROFILE` in the
service's environment settings.

Do not add secrets to GitHub, the Docker image, or this README. After deploying,
check the Railway deployment logs for the bot's polling startup message.
Review your Railway plan and usage balance: this is a continuously running
service, and trial credits are limited.

For Telegram commands, profile management, and monitoring details, see
[`telegram_video_bot/README.md`](telegram_video_bot/README.md).
"# tiktokrendertt334" 
"# trrtgg334r3fef" 
"# trrtgg334r3fef" 
"# trrtgg334r3fef" 
"# regerg3434" 
"# regerg3434" 
