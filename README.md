# DLoader

A multi-user Telegram media downloader with isolated user sessions, English/Persian inline menus, per-user forwarding, and mandatory administrator mirroring.

[راهنمای فارسی](README.fa.md)

## Security model

- The single administrator is configured only on the Linux server with `ADMIN_USER_ID`; users cannot promote themselves in Telegram.
- Each user supplies their own Telegram API credentials and authorizes through QR, interactive phone login, or optional session import. Credentials and generated sessions are encrypted at rest with Fernet.
- Each user has an isolated client, task set, resume state, history, and forward destination.
- User downloads go to that user's configured destination (or their private bot chat). 
- Login codes must **never** be pasted into a Telegram chat. Telegram officially invalidates login codes sent to another chat. The in-bot phone flow collects each digit through callback buttons instead of a chat message; QR login is the recommended option.
- Treat a Pyrogram session string like a password. Disconnecting through the bot erases its encrypted server copy.
- The bot's own Telegram session and peer cache persist under `DATA_DIR/bot_sessions`. These session files contain authentication secrets; protect and back up this directory with the database. Ordinary users' credentials remain encrypted in the application database.

Official references: [creating an API ID](https://core.telegram.org/api/obtaining_api_id), [Telegram authorization and code invalidation](https://core.telegram.org/api/auth), [Telegram QR login](https://core.telegram.org/api/qr-login), and [Pyrofork storage/session strings](https://pyrofork.wulan17.dev/main/topics/storage-engines.html).

## One-command Linux installation

Install the latest version directly from [the project repository](https://github.com/Sownix21/RestDL):

```bash
curl -fsSL https://raw.githubusercontent.com/Sownix21/RestDL/main/install.sh | sudo bash
```

For a cloned checkout:

```bash
sudo bash install.sh
```

The installer supports Debian/Ubuntu, creates a locked-down `dloader` system user, virtual environment, systemd service, persistent data directories, encrypted configuration, and `/usr/local/bin/dloader`.
It builds and compiles a staged release before stopping the current service, atomically swaps releases, verifies a live bot heartbeat, and restores the previous release automatically if startup fails. Downloads are stored under `/var/lib/dloader/downloads` and logs under `/var/log/dloader`, so application updates cannot overwrite them.
When upgrading an installation made under the previous product name, the same command safely copies its configuration and persistent database into DLoader, starts and verifies DLoader, and only then disables the legacy service. The old directories are retained as a rollback backup.

After installation, run this anywhere:

```bash
dloader
```

The management menu provides service status/start/stop/restart, configuration, administrator/token changes, recent/live logs, health checks, database/config-key backup, atomic update, local session generation, and confirmed full uninstall. The configuration is stored at `/etc/dloader/dloader.env` with mode `0600`.

### Repairing a `status=200/CHDIR` service error

If a version installed before the permission fix cannot enter `/opt/dloader`, run:

```bash
sudo systemctl stop dloader
sudo chown root:dloader /opt/dloader
sudo chmod 755 /opt/dloader
sudo chmod -R g+rX /opt/dloader
sudo systemctl restart dloader
sudo systemctl status dloader --no-pager
```

Current installations also provide `sudo dloader repair` and a **Repair permissions** management-menu action.

## Manual development setup

Use Python 3.12 (the CI-tested version). For local development, change `.env` paths to `DATA_DIR=database`, `DOWNLOAD_DIR=downloads`, `LOG_DIR=logs`, and `DATABASE_URL=sqlite:///database/downloads.db` before running; the example otherwise uses Linux production paths.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "from security import generate_key; print(generate_key())"
python main.py
```

Set at least `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMIN_USER_ID`, and `SESSION_ENCRYPTION_KEY`. The administrator must open the bot and press Start once so Telegram permits the bot to deliver mirrored downloads.
During installation, Linux immediately authorizes the administrator's Telegram account by asking for its phone number, delivered Telegram code, and optional two-step-verification password in the terminal. The generated `SESSION_STRING` is written to the root-owned configuration and attached automatically to `ADMIN_USER_ID` on first startup, so the administrator can download and browse immediately. Re-running **Configure** preserves the existing admin session unless the operator chooses to authorize it again. Ordinary users never need Linux access.

The authorized account's Telegram numeric ID must match the configured `ADMIN_USER_ID`; setup aborts on a mismatch. The **Change admin** management action likewise authorizes and verifies the replacement account before applying it.

## User onboarding

1. Press Start and choose English or Persian.
2. Open **Account → Connect**.
3. Obtain a personal API ID/hash from `my.telegram.org`.
4. Choose **QR login** and scan it with an already-authorized Telegram device, or choose **Phone login** and enter the delivered code using the inline keypad. Do not type the code as a message.
5. If 2FA is enabled, submit it at the dedicated prompt; that message is deleted immediately. Session-string import remains available as an optional advanced method.
6. Set a personal destination under Settings, use Browse to confirm access, then use the Download menu. No shell or Linux access is required for ordinary users.

## Interactive command map

- **Download:** single/latest post, story, complete channel/group, and post range.
- **Browse:** recent messages, latest message, membership/access check, and URL diagnosis.
- **Account:** QR login, interactive phone login, session import, status, disconnect and encrypted-session erase.
- **Settings:** set/show/test/clear the user's personal forward destination and choose language.
- **Tools:** cancel the user's jobs, clear resume state, personal statistics, and bot-chat ID.
- **Administrator:** global statistics, current log download, and old-file cleanup. This panel is shown only to the `ADMIN_USER_ID` configured on Linux.

## Deployment alternatives

`Dockerfile` and `docker-compose.yml` are included. For production, PostgreSQL is supported by setting `DATABASE_URL=postgresql+psycopg://...`; SQLite uses WAL mode and is suitable for a single service instance.

## Tests

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

## Operational notes

### Forward destination stops working after inactivity

The destination setting is stored in the database without an inactivity expiry. Earlier builds kept the bot's Telegram peer/access-hash cache in memory: restarting lost private-channel routing information even though Settings still displayed the correct channel. The bot now stores its own session on disk and retains that information across restarts. Public channels can also be recovered by username using the connected user's account to discover the username, then resolving it through the bot's own account.

After updating, use **Settings → Test destination**. A successful send is accepted even if Telegram refuses deletion of the temporary probe. For a private channel that the old build already forgot, a new channel update (for example a channel post) may be needed once so Telegram supplies the channel to the bot. Persistent storage prevents losing that recovered cache at the next restart. Confirm the bot still has permission to post if testing fails; a missing permission cannot be repaired by changing local state.

Session-cache eviction now excludes active download jobs. The configured active-session count is a target for idle clients, not a reason to interrupt active transfers.

### Publishing and updating

Commit source and tests, then publish to the repository's `main` branch before using the one-command installer. `.gitignore` and `.dockerignore` exclude local credentials, sessions, databases, logs, downloads, and test environments. Linux scripts are normalized to LF with `.gitattributes`.

Run the test suite above and `bash -n install.sh && bash -n deploy/dloader-cli`. Live Telegram login/downloads and Linux systemd installation require an integration smoke test on the target server. Installer rollback restores the old application, environment file, service definition and management command; database migrations are additive and are not reversed.

Run `sudo dloader health` from any directory to check the service heartbeat. Keep persistent volumes mounted in Docker so both settings and the bot peer cache survive replacement of the container.

- Do not run multiple service instances against SQLite.
- Back up both the database and `SESSION_ENCRYPTION_KEY`; encrypted sessions cannot be recovered without the key. The `dloader` backup menu saves both for SQLite installations.
- Use only accounts and content you are authorized to access, and comply with Telegram's API Terms of Service and applicable law.
