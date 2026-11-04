# Operations

Stopping, tearing down, upgrading and backing up the Docker Compose stack. For
first-time setup see the [setup guide](setup.md); for errors see its
[troubleshooting](setup.md#troubleshooting) section.

## What is stored where

| Data | Where | Survives `down` | Survives `down -v` |
|---|---|---|---|
| Studies on each cloud node | volumes `orthanc-us-data`, `orthanc-eu-data`, `orthanc-asia-data`, `orthanc-af-data` | yes | **no** |
| Instances buffered on the edge, not yet forwarded | volume `orthanc-edge-data` | yes | **no** |
| Study pins (`study:*`) and agent RTTs (`rtt:*`) | volume `redis-data` (Redis append-only file) | yes | **no** |
| Node telemetry (`node:*`) | Redis, expires after `REDIS_TTL_S`; the Telemetry Daemon rewrites it | rebuilt | rebuilt |
| `.env`, `certs/` | your working copy, not a volume | yes | yes |

The Orthanc volumes are mounted at `/var/lib/orthanc/db` (`StorageDirectory` in
`config/orthanc/*.template.json`). Compose normally prefixes the volume names
with the project name (the directory name); `docker volume ls` shows the real
ones.

## Stop and tear down

```bash
docker compose stop        # stop the containers, keep everything
docker compose down        # remove containers and the diomede-net network, keep volumes
docker compose down -v     # ALSO delete the volumes above
```

`down -v` deletes every stored study, the instances the edge has not forwarded
yet, and all study pins. Use it only when you want an empty stack. A plain `down` followed by `up -d` brings the data back.

## Upgrade

```bash
git pull
diff .env.example .env
docker compose build orchestrator telemetry-daemon agent-001
docker compose up -d
```

1. **Compare `.env` with `.env.example`.** `.env` is not updated by `git pull`.
   A required key that is missing from your `.env` makes a service exit with
   `<NAME> environment variable must be set`. Add the new keys and keep your own
   values (the diff also lists the lines you changed on purpose).
2. **Rebuild every image that bakes in `src/`.** `orchestrator` and
   `telemetry-daemon` are two services built from the same Dockerfile
   (`config/orchestrator/Dockerfile`), so each has its own image: rebuilding only
   `orchestrator` leaves the daemon running the old code. The edge agent has its
   own image (`agent-001`).
3. **Recreate the containers.** `docker compose up -d` recreates the ones whose
   image or configuration changed and keeps the volumes.
4. **Check.** `docker compose ps` should show every service `Up`, and
   `curl -k https://localhost:8000/health` should return `{"status":"ok"}`. A
   non-empty `curl -k -H "X-API-Key: <key>" https://localhost:8000/nodes` shows
   the daemon is writing telemetry.

If you re-run `scripts/gen_certs.sh`, it deletes everything in `certs/` and
creates a new CA, so recreate the containers afterwards:
`docker compose up -d --force-recreate`.

## Back up

Keep `.env` and `certs/` (including `certs/ca.key`) somewhere safe; they are
gitignored and cannot be recovered from the repository. To back up a volume, stop
the stack first so nothing writes to it, then archive each volume from a throwaway
container that mounts the volumes of the service it belongs to. Going through the
container avoids guessing the volume name, which Compose prefixes with the project
name:

```bash
docker compose stop
docker run --rm --volumes-from redis -v "$PWD":/backup alpine \
  tar czf /backup/redis-data.tgz -C /data .
docker run --rm --volumes-from orthanc-us -v "$PWD":/backup alpine \
  tar czf /backup/orthanc-us-data.tgz -C /var/lib/orthanc/db .
docker compose start
```

Repeat the second `docker run` for `orthanc-eu`, `orthanc-asia`, `orthanc-af` and
`agent-001` (the edge buffer); they all mount their data at `/var/lib/orthanc/db`
(see `docker-compose.yml`).

To restore, run `docker compose create` (it creates the containers and empty
volumes without starting them), run the same commands with
`tar xzf /backup/<file>.tgz -C <path>` instead of `tar czf ...`, then
`docker compose up -d`.

These Docker commands were written from `docker-compose.yml` and Docker's
documented behaviour and have **not been run**; try a restore on a copy of your
data before you rely on it.
