"""
edge/orthanc_source.py – DicomSource backed by the Edge Orthanc REST API.

Polls GET /series?expand for buffered instances grouped by Orthanc study ID
(optionally only studies Orthanc marks stable), streams raw DICOM bytes via
GET /instances/{id}/file, and acknowledges by deleting the local copy.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv

from src.edge.transport import DicomSource
from src.utils.env import require_env
from src.utils.logging_config import get_logger

log = get_logger(__name__, "ORTHANC_SOURCE")
load_dotenv()

_EDGE_BASE = require_env("EDGE_BASE")
_EDGE_AUTH = (require_env("EDGE_USER"), require_env("EDGE_PASS"))
# Route a study only once Orthanc marks it stable (no new instance for StableAge seconds).
_WAIT_FOR_STABLE_STUDY = os.getenv("FORWARDER_WAIT_FOR_STABLE_STUDY", "false").lower() == "true"
_STORAGE_LEVELS = (80, 95)


class OrthancSource(DicomSource):
    """Reads new DICOM instances from a co-located Edge Orthanc via its REST API."""

    def __init__(
        self,
        base: str = _EDGE_BASE,
        auth: tuple[str, str] = _EDGE_AUTH,
        wait_for_stable_study: bool = _WAIT_FOR_STABLE_STUDY,
    ) -> None:
        self._base = base.rstrip("/")
        self._auth = auth
        self._wait_for_stable_study = wait_for_stable_study
        self._last_seq: int = 0
        self._quota_mb: int | None = None
        self._storage_mode = ""
        self._storage_level = 0

    async def poll_new(self, client: httpx.AsyncClient) -> dict[str, list[str]]:
        """Return all instance IDs in the Edge Orthanc buffer, keyed by Orthanc study ID.

        The Orthanc study ID is an opaque hash, not the StudyInstanceUID, so it is
        safe to log and to pass to the orchestrator. Costs one request per poll, or two
        when waiting for stable studies, however many studies are buffered.
        """
        resp = await client.get(f"{self._base}/series?expand", auth=self._auth, timeout=10)
        resp.raise_for_status()
        studies: dict[str, list[str]] = {}
        for series in resp.json():
            studies.setdefault(series["ParentStudy"], []).extend(series["Instances"])

        if self._wait_for_stable_study and studies:
            resp = await client.get(f"{self._base}/studies?expand", auth=self._auth, timeout=10)
            resp.raise_for_status()
            stable = {study["ID"] for study in resp.json() if study.get("IsStable")}
            studies = {sid: ids for sid, ids in studies.items() if sid in stable}

        studies = {sid: ids for sid, ids in studies.items() if ids}
        log.info(
            "New instances: %d in %d studies",
            sum(len(ids) for ids in studies.values()),
            len(studies),
        )
        await self._check_storage(client)
        return studies

    async def _check_storage(self, client: httpx.AsyncClient) -> None:
        """Log when the edge buffer crosses a fill level. Never raises, so polling carries on."""
        try:
            if self._quota_mb is None:
                resp = await client.get(f"{self._base}/system", auth=self._auth, timeout=10)
                resp.raise_for_status()
                system = resp.json()
                self._quota_mb = int(system.get("MaximumStorageSize") or 0)
                self._storage_mode = system.get("MaximumStorageMode", "Recycle")
                if self._quota_mb and self._storage_mode != "Reject":
                    log.warning(
                        "Edge Orthanc recycles its oldest instances when its %d MB storage is "
                        "full, including ones not forwarded yet; set MaximumStorageMode to Reject",
                        self._quota_mb,
                    )
            if not self._quota_mb:
                return
            resp = await client.get(f"{self._base}/statistics", auth=self._auth, timeout=10)
            resp.raise_for_status()
            used_mb = float(resp.json()["TotalDiskSizeMB"])
        except Exception as exc:
            log.warning("Edge storage check failed: %s", exc)
            return

        percent = 100 * used_mb / self._quota_mb
        level = max((lvl for lvl in _STORAGE_LEVELS if percent >= lvl), default=0)
        if level == self._storage_level:
            return
        self._storage_level = level
        if level == 95:
            once_full = (
                "new instances will be rejected"
                if self._storage_mode == "Reject"
                else "the oldest instances will be recycled, forwarded or not"
            )
            log.error(
                "Edge storage %.0f%% full (%.0f of %d MB); once it is full %s",
                percent,
                used_mb,
                self._quota_mb,
                once_full,
            )
        elif level:
            log.warning(
                "Edge storage %.0f%% full (%.0f of %d MB)", percent, used_mb, self._quota_mb
            )
        else:
            log.info(
                "Edge storage back to %.0f%% (%.0f of %d MB)", percent, used_mb, self._quota_mb
            )

    @asynccontextmanager
    async def open_stream(
        self, client: httpx.AsyncClient, instance_id: str
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream raw DICOM bytes for instance_id from Edge Orthanc.

        Uses httpx streaming so the file is never fully buffered in memory -- the
        caller pipes the yielded iterator straight to the destination node.
        """
        async with client.stream(
            "GET",
            f"{self._base}/instances/{instance_id}/file",
            auth=self._auth,
            timeout=60,
        ) as resp:
            resp.raise_for_status()
            yield resp.aiter_bytes()

    async def acknowledge(self, client: httpx.AsyncClient, instance_id: str) -> None:
        """Delete the instance from Edge Orthanc to prevent disk fill."""
        resp = await client.delete(
            f"{self._base}/instances/{instance_id}",
            auth=self._auth,
            timeout=10,
        )
        resp.raise_for_status()
        log.info("instance=%s deleted from edge buffer", instance_id)
