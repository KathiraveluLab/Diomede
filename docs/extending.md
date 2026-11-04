# Extending Diomede

Two parts of the system are built to be replaced: how the orchestrator ranks
nodes (a `NodeScorer`) and how the edge agent reads instances from its buffer
(a `DicomSource`). This page lists the steps and the pitfalls for each.

## Adding a scorer

The orchestrator ranks the healthy nodes with one scorer and returns the highest
score. Scorers register themselves by name in `src/orchestrator/scorer.py`.

1. **Write the class.** Create `src/orchestrator/<name>_scorer.py` with a
   `NodeScorer` subclass whose `score(node)` returns a float (higher is preferred),
   and register it at the bottom of the module, as
   [`weighted_scorer.py`](../src/orchestrator/weighted_scorer.py) does:

   ```python
   from typing import Any

   from .scorer import _REGISTRY, NodeScorer


   class LowestQueueScorer(NodeScorer):
       def score(self, node: dict[str, Any]) -> float:
           return 1.0 / ((node.get("queue_size") or 0) + 1)


   _REGISTRY["lowest-queue"] = LowestQueueScorer
   ```

2. **Import the module from `src/orchestrator/main.py`**, in the relative-import
   block with the `WeightedScorer` import. Registration happens when the module is
   imported, and nothing else imports your file, so without this line the name is
   never registered. Keep the block sorted: ruff puts `.lowest_queue_scorer` before
   `.scorer`, and `ruff check --fix` does it for you.

   ```python
   from .lowest_queue_scorer import LowestQueueScorer  # noqa: F401 to trigger self-registration
   ```

3. **Select it** by setting `SCORER=lowest-queue` in `.env`. The default is
   `weighted`.

4. **Rebuild and recreate the orchestrator**, because the image copies `src/orchestrator` and
   `src/utils`: `docker compose up -d --build orchestrator`. The Telemetry Daemon does not use
   the scorer.

What the scorer receives and must handle:

- Only nodes with `healthy: true` are scored (`get_best_node` in `main.py`).
- Each `node` is the dict the Telemetry Daemon stored in Redis: `node_id`,
  `ae_title`, `base_url`, `queue_size`, `disk_free_mb`, `disk_total_mb`,
  `instance_count`, `healthy`, `ts`. `disk_free_mb` and `disk_total_mb` are `None`
  for a node without a storage quota (`poll_node` in `daemon.py`).
- `rtt_ms` is added only when the requesting agent has an RTT measurement for that
  node. Treat it as optional; `WeightedScorer` assumes 250 ms when it is missing.
- The same dict is turned into the response. `score` in the response is filled only
  if your scorer stores it with `node["score"] = ...` (as `WeightedScorer` does);
  otherwise the field is `null`, and the forwarder logs `score=0.0000`.

Behaviour to be aware of:

- **A wrong name stops the orchestrator.** `get_scorer()` runs at startup
  (in `lifespan()` in `main.py`) and raises `ValueError: Unknown scorer '<name>'. Registered: [...]`.
  The containers use `restart: unless-stopped`, so a typo in `SCORER` (or a missing
  import from step 2) shows up as a restart loop; read
  `docker compose logs orchestrator`.
- **One instance per process.** The scorer is created once, with no arguments
  (`cls()`), and reused for every request. Read configuration from the environment
  in `__init__`, as `WeightedScorer` does for `W_QUEUE`, `W_DISK` and `W_RTT`. In unit
  tests reset the cache with `monkeypatch.setattr(scorer_module, "_SCORER_INSTANCE", None)`
  (see `tests/unit/test_orchestrator.py`).
- `src/benchmark/routing_correctness.py` computes its expected winners with
  `WeightedScorer`, so it only agrees with an orchestrator running the default
  scorer (see [benchmarks](benchmarks.md)).

## Adding a DicomSource

The edge forwarder reads instances through the `DicomSource` interface in
[`src/edge/transport.py`](../src/edge/transport.py). The only implementation,
`OrthancSource`, reads the co-located edge Orthanc over REST.

1. **Implement the three methods** in a new module in `src/edge/`:

   | Method | Contract |
   |---|---|
   | `async poll_new(client)` | Return `{study_id: [instance_id, ...]}` for instances ready to route. Study IDs must be opaque (never DICOM UIDs or other PHI), because they are logged and sent to the orchestrator. The forwarder sends all instances of one study to the node the orchestrator pins it to. Study IDs are sent as the `study_id` query parameter, which the orchestrator accepts only if it matches `^[A-Za-z0-9.\-]+$` and is at most 128 characters; any other ID gets a 422 and the study is never routed. |
   | `open_stream(client, instance_id)` | Return an async context manager that yields an async iterator of the raw DICOM bytes. Decorate an async generator with `@asynccontextmanager`, as `OrthancSource` does. The bytes are piped to the destination without being buffered. |
   | `async acknowledge(client, instance_id)` | Remove the instance from the buffer. It is called only after a confirmed upload. If it raises, the forwarder logs a warning and retries the delete on later polls without uploading again. |

   `tests/unit/test_forwarder.py` has a small `_StubSource` you can copy for tests.

2. **Wire it in.** There is no setting that selects a source. `forwarder.run()`
   creates `OrthancSource()` when it is called without one,
   and the container starts it as `python -m src.edge.forwarder` (`config/edge/start.sh`),
   which calls `run()` with no argument. Pass your source in the `run(...)` call in
   `forwarder.py`'s `__main__` block, or replace the default there.

3. **Rebuild the edge image**: `docker compose up -d --build agent-001`.

Pitfalls:

- `forwarder.py` imports `OrthancSource` at module level, and `orthanc_source.py`
  reads `EDGE_BASE`, `EDGE_USER` and `EDGE_PASS` when it is imported. They must stay
  set even if your source does not use them.
- The edge image copies only `src/edge` and `src/utils`, and installs
  `config/edge/requirements.txt`. Put your module in `src/edge/` and add any new
  dependency to that file and to the `edge` extra in `pyproject.toml` so host-side
  tests can import it.
- An exception from `open_stream` is handled like a failed upload: the instance and
  the rest of its study stay in the buffer for the next poll. Do not let it raise an
  `httpx.HTTPStatusError` with status 400, 413, 415 or 422 for a read problem; the
  forwarder reads those as the cloud node rejecting the file and holds the instance
  back for an hour (`_REJECTED_RETRY_S`).
