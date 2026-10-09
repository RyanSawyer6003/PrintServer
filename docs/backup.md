# Backups

`scripts/backup.sh` backs the server up with [restic](https://restic.net) and restores it. restic runs in a throwaway container, so the Docker host needs nothing installed. Snapshots are encrypted and deduplicated.

## What is saved

| Item | Holds |
|---|---|
| `cups_config` volume | Queues, and the server certificate with its private key |
| `usage_data` volume | Usage history and per-person allowances |
| `.env` | Every deployment setting |

The other volumes (spool, logs, report pages) are rebuilt by the server and are left out.

## Set up

1. **Decide where the snapshots go** and make that place available on the Docker host. For an external drive or a network share, mount it, with an `/etc/fstab` entry so it comes back after a reboot:

   ```bash
   sudo mkdir -p /mnt/backup
   sudo mount <device or share> /mnt/backup
   sudo mkdir /mnt/backup/printserver
   ```

2. **Add two lines to `.env`:**

   ```
   RESTIC_REPOSITORY=/mnt/backup/printserver
   RESTIC_PASSWORD=<a long random password>
   ```

   Save the password somewhere other than this server. `.env` is inside the backup, so without the password the backup can't be opened.

3. **Create the repository, take the first backup, and read it back:**

   ```bash
   sudo scripts/backup.sh init
   sudo scripts/backup.sh run
   sudo scripts/backup.sh check
   ```

4. **Schedule it:**

   ```bash
   sudo scripts/backup.sh install-timer          # every night at 02:15
   sudo scripts/backup.sh install-timer 23:30    # or pick a time
   ```

## What a run does

- Stops the `usage` service for the few seconds the copy takes, so its database is consistent, then starts it again. Printing is not interrupted: `usage` is not in the print path, and `cups_config` is copied live.
- Takes a snapshot tagged `nightly` and with the deployed commit.
- Thins out the nightly snapshots: 14 daily, 8 weekly, 12 monthly. Change these with `BACKUP_KEEP_DAILY`, `BACKUP_KEEP_WEEKLY` and `BACKUP_KEEP_MONTHLY` in `.env`.

A snapshot with any other tag is kept until someone removes it. Use that before risky work:

```bash
sudo scripts/backup.sh run --tag pre-upgrade
```

## The timer

The timer is a systemd unit named `printserver-backup`. The time is the host's local time. A run that was missed because the host was off happens at the next boot.

```bash
systemctl list-timers printserver-backup.timer      # next and last run
journalctl -u printserver-backup.service -n 50      # what the last runs printed
sudo systemctl start printserver-backup.service     # run now
sudo scripts/backup.sh remove-timer
```

Run `install-timer` again if the repository directory moves, because the unit holds the script's path.

## Monitoring

```bash
scripts/backup.sh status        # last success within 36 hours?
scripts/backup.sh status 60     # or another limit, in hours
```

| Exit code | Meaning |
|---|---|
| 0 | A backup succeeded within the limit |
| 1 | The last success is older than the limit |
| 2 | No successful backup is recorded on this host |

It prints one line and needs no access to the repository, so it suits a scheduled check from a monitoring or RMM agent. It reports what this host recorded. `check` is what proves the repository itself is readable; run it every month or so.

## Restore

A restore puts both volumes back as they were in the snapshot, including the server certificate. The stack must be stopped.

### Onto the same server

```bash
docker compose stop
sudo scripts/backup.sh snapshots                  # pick one, or use the latest
sudo scripts/backup.sh restore --force            # or: restore <snapshot ID> --force
docker compose up -d
```

`--force` is required because the volumes hold data. Anything in them that the snapshot doesn't hold is removed. `.env` is left alone when it exists; to see the snapshot's copy, run `sudo scripts/backup.sh restic dump latest /data/env/.env`.

### Onto a rebuilt server

Install Docker, mount the backup location, and clone this repository. `.env` doesn't exist yet, so give the two settings on the command line:

```bash
sudo RESTIC_REPOSITORY=/mnt/backup/printserver RESTIC_PASSWORD='<password>' scripts/backup.sh restore
docker compose up -d --build
```

This restores `.env` first, then creates and fills both volumes. Restore **before** the first `docker compose up`. If the stack has already been started, CUPS has an empty configuration and may have issued itself a new certificate: stop it and restore with `--force`.

If the backup location is mounted at a different path than before, correct `RESTIC_REPOSITORY` in the restored `.env`. Then schedule the timer again.

### After a restore

- The checks in the README's Routing section pass.
- The queues are listed, and a test page prints from a client.
- `/usage/` shows the jobs from before the restore.
- The server certificate's fingerprint is the one clients already trust.

## Check a backup on another machine

A repository is an ordinary directory, and restic is a single program for Linux, Windows and macOS. To prove a backup is usable before relying on it, open it somewhere else:

```bash
restic -r <path to the repository> check --read-data
restic -r <path to the repository> restore latest --target <empty directory>
```

It asks for the password. The restored tree has `data/cups_config/printers.conf` (the queues), `data/usage_data/` and `data/env/.env`.

## Move to a different target

Change `RESTIC_REPOSITORY` in `.env`, then run `init` and `run` again. The old repository stays readable with the same password.

To bring the old snapshots across, name the old location as the source:

```bash
sudo RESTIC_FROM_REPOSITORY=<old location> RESTIC_FROM_PASSWORD='<password>' scripts/backup.sh restic copy
```

## Targets

- **A path on the host** (an external drive, or a mounted network share). It must be an absolute path. The script refuses a path on the same disk as the Docker volumes, which is what an unmounted drive looks like. For a path on that disk on purpose, set `BACKUP_ALLOW_SAME_DISK=1` in `.env`.
- **An SMB share.** restic's documentation advises against SMB repositories on older Linux kernels and gives a workaround, which the script applies when it sees an SMB mount. NFS avoids the question.
- **Object storage** (`s3:...`, `b2:...`) **or a restic REST server** (`rest:...`). Put the provider's credentials in `.env` under restic's own names, such as `AWS_ACCESS_KEY_ID` and `B2_ACCOUNT_KEY`. Every `RESTIC_*`, `AWS_*` and `B2_*` value is passed to restic.
- **SFTP** is not supported as written: the container has no SSH key.

## Things to know

- **`.env` values reach the `cups` container.** Compose hands that container everything in `.env`, so it also sees `RESTIC_PASSWORD` and any storage credentials. With a path on the host this gains an intruder nothing: the container can't reach the repository, and the snapshots hold only what the container already has. With cloud storage, use a key that can't delete, so a compromised server can't remove its own backups. Retention then has to run from somewhere else.
- **The timer runs the script as root from this checkout.** Anyone who can change the files here can run commands as root.
- **The restic version is pinned** in `scripts/backup.sh` (`DEFAULT_IMAGE`). Raise it by hand from time to time. `BACKUP_IMAGE` in `.env` overrides it.
- **An interrupted run** can leave a lock on the repository. If a later run says the repository is locked, and nothing else is using it, clear it with `sudo scripts/backup.sh restic unlock`.
