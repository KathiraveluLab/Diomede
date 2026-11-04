# Orchestrator API

Reference for the HTTP API served by `src/orchestrator/main.py` on
`https://localhost:8000` (`ORCHESTRATOR_HTTPS_PORT`). The Forwarder Daemon uses it to
pick a destination node and report its RTT probes. The examples use the CA from
`scripts/gen_certs.sh` (`--cacert certs/ca.pem`; the certificate also covers `localhost`)
and assume the stack from the [setup guide](setup.md) is running.

The live, interactive version is the Swagger UI at <https://localhost:8000/docs>
(also `/redoc` and `/openapi.json`). It lists the parameters and models; this page adds
what they mean and what each error looks like.

## Authentication

`/get-best-node`, `/nodes` and `/heartbeat` need an `X-API-Key` header holding the value of
`ORCHESTRATOR_API_KEY`. `/health`, `/docs`, `/redoc` and `/openapi.json` need no key.

| Request | Status | Body |
|---|---|---|
| No `X-API-Key` header | 401 | `{"detail":"Not authenticated"}` |
| Wrong key | 401 | `{"detail":"Invalid or missing API Key"}` |

## `GET /get-best-node`

Returns the highest-scoring healthy node or, with `study_id`, the node the study is pinned to
(see [Architecture](architecture.md#4-scoring-algorithm)).

| Query parameter | Required | Meaning |
|---|---|---|
| `agent_id` | yes | The asking agent, e.g. `agent-001`. Its RTTs (from `/heartbeat`) feed the score. |
| `study_id` | no | The **Orthanc** study ID, never a DICOM StudyInstanceUID. Up to 128 characters from `A-Z a-z 0-9 . -`. The first answer is pinned until `STUDY_AFFINITY_TTL_S` (default 3600 s) after the study's last request, so later instances get the same node while it stays healthy. |
| `exclude` | no | A node ID to skip. Repeat it for several. Excluding the node a study is pinned to moves the study. |

```bash
curl --cacert certs/ca.pem -H "X-API-Key: <your_api_key>" "https://localhost:8000/get-best-node?agent_id=agent-001&study_id=0a1b2c3d-4e5f6a7b-8c9d0e1f-2a3b4c5d-6e7f8a9b"
```

Response `200`: the `/nodes` fields below plus `rtt_ms` (this agent's RTT to the node, `null` if
it has not sent one), `score`, `study_id` (`null` if none was sent) and `rerouted`
(`true` only on the answer that moved a study off an unavailable or excluded node).

## `GET /nodes`

Latest telemetry of every node whose telemetry has not expired (`REDIS_TTL_S`, 30 s in `.env.example`),
healthy or not. It can be an empty list.

```bash
curl --cacert certs/ca.pem -H "X-API-Key: <your_api_key>" https://localhost:8000/nodes
```

| Field | Meaning |
|---|---|
| `node_id`, `ae_title`, `base_url` | Region name, DICOM AE title and REST base URL of the node |
| `queue_size` | Pending plus running jobs on the node |
| `disk_free_mb`, `disk_total_mb` | Free space and storage quota; `null` when the node has no quota |
| `instance_count` | Instances stored on the node |
| `healthy` | `false` when the node is unreachable or nearly full |
| `ts` | When the Telemetry Daemon wrote this record (ISO 8601, UTC) |

A node the daemon could not reach has `healthy: false` and `null` for `queue_size`, the disk
fields and `instance_count`.

## `POST /heartbeat`

Stores an agent's RTT measurements (milliseconds) for `RTT_TTL_S` (default 10800 s),
replacing its previous ones. Responds `204` with no body.

```bash
curl --cacert certs/ca.pem -H "X-API-Key: <your_api_key>" -H "Content-Type: application/json" -d '{"agent_id":"test-agent","rtt_dict":{"us-east1":42.5,"eu-west1":120}}' https://localhost:8000/heartbeat
```

`rtt_dict` maps node IDs (the `node_id` values, e.g. `us-east1`) to RTTs. An unknown node ID,
or an RTT that is zero, negative or not finite, is a `422`. An empty `rtt_dict` is accepted
and clears the agent's RTTs.

## `GET /health`

Returns `{"status":"ok"}` while the process is up. It does not check Redis and needs no key.

```bash
curl --cacert certs/ca.pem https://localhost:8000/health
```

## Errors

| Status | When | Body |
|---|---|---|
| 422 | Missing `agent_id`, bad `study_id`, or a bad `/heartbeat` body | `{"detail":[{"type":…,"loc":[…],"msg":…}]}` |
| 503 | `/get-best-node`: no healthy node is available (after applying `exclude`) | `{"detail":"No healthy nodes available"}` |
| 503 | Redis cannot be reached | `{"detail":"Telemetry store unavailable"}` |
| 503 | The lifespan never ran (e.g. `uvicorn --lifespan off`) | `{"detail":"Redis client not initialized"}` |
| 503 | `/get-best-node` with `study_id`: other agents kept changing the study's pin | `{"detail":"Study assignment is changing, retry later"}` |

A `503` is temporary; the Forwarder leaves the study's instances on the edge and asks again on its next poll.
