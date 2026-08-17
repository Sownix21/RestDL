# RestrictiveDL

A multi-user Telegram media downloader with isolated user sessions, English/Persian inline menus, per-user forwarding, and mandatory administrator mirroring.

## Security model

- The single administrator is configured only on the Linux server with `ADMIN_USER_ID`; users cannot promote themselves in Telegram.
- Each user supplies their own Telegram API credentials and Pyrogram session. Credentials and sessions are encrypted at rest with Fernet.
- Each user has an isolated client, task set, resume state, history, and forward destination.
- User downloads go to that user's configured destination (or their private bot chat). 
- Login codes must **never** be pasted into a Telegram chat. Telegram officially invalidates login codes sent to another chat. Generate a session locally with `scripts/generate_session.py`; the terminal flow supports Telegram-delivered codes and two-step-verification passwords.
- Treat a Pyrogram session string like a password. Disconnecting through the bot erases its encrypted server copy.

Official references: [creating an API ID](https://core.telegram.org/api/obtaining_api_id), [Telegram authorization and code invalidation](https://core.telegram.org/api/auth), [Telegram QR login](https://core.telegram.org/api/qr-login), and [Pyrofork storage/session strings](https://pyrofork.wulan17.dev/main/topics/storage-engines.html).

## One-command Linux installation

Install the latest version directly from [Sownix21/RestDL](https://github.com/Sownix21/RestDL):

```bash
curl -fsSL https://raw.githubusercontent.com/Sownix21/RestDL/main/install.sh | sudo bash
```

For a cloned checkout:

```bash
sudo bash install.sh
```

The installer supports Debian/Ubuntu, creates a locked-down `restdl` system user, virtual environment, systemd service, persistent data directories, encrypted configuration, and `/usr/local/bin/restdl`.

After installation, run this anywhere:

```bash
restdl
```

The management menu provides service status/start/stop/restart, configuration, administrator/token changes, recent/live logs, database backup, update, local session generation, and confirmed full uninstall. The configuration is stored at `/etc/restdl/restdl.env` with mode `0600`.

### Repairing a `status=200/CHDIR` service error

If a version installed before the permission fix cannot enter `/opt/restdl`, run:

```bash
sudo systemctl stop restdl
sudo chown root:restdl /opt/restdl
sudo chmod 755 /opt/restdl
sudo chmod -R g+rX /opt/restdl
sudo systemctl restart restdl
sudo systemctl status restdl --no-pager
```

Current installations also provide `sudo restdl repair` and a **Repair permissions** management-menu action.

## Manual development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "from security import generate_key; print(generate_key())"
python main.py
```

Set at least `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMIN_USER_ID`, and `SESSION_ENCRYPTION_KEY`. The administrator must open the bot and press Start once so Telegram permits the bot to deliver mirrored downloads.

## User onboarding

1. Press Start and choose English or Persian.
2. Open Account → Connect/import session.
3. Obtain a personal API ID/hash from `my.telegram.org`.
4. On a trusted computer run `python scripts/generate_session.py`. Enter the code and optional 2FA password only in that local terminal. The project uses the maintained Pyrofork implementation, including current Telegram story support.
5. Import the resulting session string through the bot. Sensitive setup messages are deleted when Telegram permits it.
6. Set a personal destination under Settings, then use the Download menu.

## Deployment alternatives

`Dockerfile` and `docker-compose.yml` are included. For production, PostgreSQL is supported by setting `DATABASE_URL=postgresql+psycopg://...`; SQLite uses WAL mode and is suitable for a single service instance.

## Tests

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

## Operational notes

- Do not run multiple service instances against SQLite.
- Back up both the database and `SESSION_ENCRYPTION_KEY`; encrypted sessions cannot be recovered without the key.
- Use only accounts and content you are authorized to access, and comply with Telegram's API Terms of Service and applicable law.
