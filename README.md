# VJ Auto Rename Bot

This project adapts the existing VJ-Join-Request-Acceptor-Bot repository into a Telegram video/document rename workflow. It retains the original bot-token client for private commands and the interactive `/login` and `/logout` flow, which saves a Telegram user-account session string in MongoDB. The saved owner account is restored after restart and is used to monitor source channels and converse with the configured external rename bot.

## Requirements

Use Python 3.10 or newer, MongoDB, Telegram API credentials from [my.telegram.org](https://my.telegram.org), a command-bot token from [@BotFather](https://t.me/BotFather), and the username of the external rename bot. The logged-in user account must already be able to read each configured source channel and must be an administrator with posting rights in each output channel. The user session and Bot API token are separate credentials; neither replaces the other.

## Configuration

Copy `.env.example` to `.env` and replace every placeholder. The application loads `.env` automatically for local runs. Never commit `.env` or session strings. Deployment platforms should provide the same values through their secret/environment-variable interface.

| Variable | Required | Purpose |
| --- | --- | --- |
| `API_ID`, `API_HASH` | Yes | Telegram MTProto application credentials used for the account login/session. |
| `BOT_TOKEN` | Yes | Bot API token used for commands and admin notices. |
| `DB_URI` | Yes | MongoDB connection URI. |
| `DB_NAME` | No | Database name; defaults to the original `vjjoinrequetbot` database so existing user sessions remain accessible. |
| `ADMINS` | Yes for admin features | Comma-separated numeric Telegram user IDs allowed to configure the service. |
| `SESSION_OWNER_ID` | No | User ID whose saved account session is used for monitoring; defaults to the first ID in `ADMINS`. |
| `RENAME_BOT_USERNAME` | Yes for automation | External rename bot username, without `@`. |
| `RENAME_RESPONSE_TIMEOUT` | No | Rename-bot response timeout in seconds; default `300`. |
| `MAX_RENAME_ATTEMPTS` | No | Bounded pre-publish attempts; default `3`, maximum `5`. |
| `LOG_CHANNEL` | No | Optional channel for new-user log notices. |

## Install and run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env, then export its values using your deployment platform or shell.
python3 bot.py
```

The existing `Procfile` starts one worker with `python3 bot.py`. Run a single worker/replica: multiple independent workers would each register Telegram handlers and could compete for the same saved account session. No Dockerfile existed in the source repository, so this project does not add an unrelated container configuration. A platform that requires a health-check web process can run the existing Flask app separately, but the bot worker remains the process defined in `Procfile`.

## First-time account login

Start the command bot and send it `/login` privately from the account specified by `SESSION_OWNER_ID` (or the first configured admin if that variable is omitted). Follow the phone number, Telegram login code, and optional two-step verification prompts. The session string is written to MongoDB by the pre-existing login path and restored on the next bot startup. Use `/logout` from that same account to stop its monitor and clear the stored session. `/status` never prints the session string.

If the database already contains a valid saved session for the configured owner, the bot attempts to restore it at startup and does not ask the user to log in again. If it has expired or is revoked, use `/logout` and `/login` to replace it.

## Configure sources and output

1. As an administrator, send `/set SOURCE_CHANNEL_ID OUTPUT_CHANNEL_ID`, for example `/set -1001111111111 -1002222222222`.
2. The logged-in account is used to validate access to the source and posting privileges in the output channel.
3. Send a thumbnail photo when prompted. It is normalized to a JPEG no larger than 320×320 pixels and 190 KB, then stored separately against that source channel in MongoDB. A later `/set` for the same source safely updates its output mapping while retaining its previous thumbnail until a replacement is submitted.
4. Configure the persistent suffix with `/setusername @Sitaratoons`.

Each source channel has its own output mapping and thumbnail. Only new video and document messages from configured source channels are processed.

## Rename workflow

The account session sends the saved thumbnail to `RENAME_BOT_USERNAME`, then copies the source media to that bot. The service waits for a response containing the old filename, appends the configured username before the extension without adding it twice, replies to the filename-prompt message, reads the returned inline keyboard, verifies that at least two buttons are present, and submits the callback data of the **second button by position** using Pyrogram's callback-answer method. It then waits for the renamed media and sends it to that source's configured output channel. If the returned file supports Telegram's custom-thumbnail parameters, the saved thumbnail is applied; otherwise the media is forwarded without claiming a thumbnail was set.

The second button is chosen strictly by keyboard position, not its visible text. The screenshot provided with the specification shows buttons ordered `DOCUMENT`, then `VIDEO`, but this project does not assume other response details are fixed: it waits for actual rename-bot messages and actual reply markup at runtime.

## Commands

| Command | Access | Description |
| --- | --- | --- |
| `/start` | All users | Shows the bot introduction. |
| `/login` | Existing login flow | Authenticates a Telegram user account and saves its session. |
| `/logout` | Existing login flow | Clears the caller's saved session; stops monitoring if it is the configured session owner. |
| `/set SOURCE_CHANNEL_ID OUTPUT_CHANNEL_ID` | Admin | Validates and saves a source/output mapping, then asks for that source's thumbnail. |
| `/setusername @username` | Admin | Stores the filename suffix persistently. |
| `/status` | Admin | Shows mapping/thumbnail status, login status, and job counts without secrets. |
| `/cancel` | Admin | Cancels active rename work; an in-progress `/set` can be cancelled at its thumbnail prompt. |

The previous join-request acceptance and auto-approval handlers and `/accept` command have been removed. `/broadcast` remains available from the original project for compatible administrator broadcast usage.

## Persistence and recovery

MongoDB stores existing user/session records plus separate channel configuration, filename suffix, and processing-job collections. Each source-channel/message pair has a unique database index. Jobs record output channel, status, attempt count, final filename, final output message ID, and failure details. Interrupted pre-publish work can be retried up to the configured limit. A job interrupted during output publishing is fenced as failed instead of being blindly resent, so an administrator should inspect the output channel before deciding what to do.

MongoDB's default document limit is a consideration for large custom thumbnails: the photo is stored in the channel configuration document, so use a reasonably small thumbnail image. The handler logs actionable failures while avoiding session-string logging.

## Deployment checklist

- Install the pinned compatible Pyrogram fork and project dependencies with `pip install -r requirements.txt`.
- Set the environment variables in the deployment platform; do not commit real `.env` values.
- Ensure MongoDB is reachable and the platform permits outbound Telegram connections.
- Run one `worker: python3 bot.py` process only.
- Log in once as `SESSION_OWNER_ID` and confirm `/status` reports the connection.
- Configure the external bot username, mappings, thumbnails, and filename suffix.
- Ensure the logged-in account can read each source and can post as an administrator to each output channel.
- Send a controlled test video/document and verify rename-bot prompts, callback selection, output file type, and thumbnail behavior before production use.

## Integration limitations

The supplied screenshot confirms the displayed filename prompt and a two-button `DOCUMENT`/`VIDEO` order, but it does not reveal the external bot username, full response sequence, or whether it accepts a standalone thumbnail before the media. The target external bot was not identified or tested live in this environment. Therefore, set `RENAME_BOT_USERNAME` to the actual bot and perform a controlled integration test. If the actual bot does not accept a separate thumbnail message or returns its keyboard in a different way, the interaction adapter will need adjustment based on its observed behavior. The project has no Telegram credentials or MongoDB deployment credentials, so live account login, database behavior against the user's server, actual callback execution, and publication permissions cannot be certified here.
