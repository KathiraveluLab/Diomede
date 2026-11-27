"""Edge-to-cloud DICOM forwarding transports and DICOM-path probing.

Supports three forwarding protocols between an Edge Agent and regional cloud Orthanc nodes:
  - ``rest``:    Zero-copy streaming HTTP POST /instances (Content-Type: application/dicom)
  - ``stow-rs``: Standard DICOMweb STOW-RS multipart/related POST /dicom-web/studies
  - ``dimse``:   Native DIMSE-TLS C-STORE via Edge Orthanc (/modalities/{node_id}/store)
                 paired with optional DICOM Storage Commitment (IHE RAD-10 N-ACTION /
                 N-EVENT-REPORT via /modalities/{node_id}/storage-commitment).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx

from src.edge.transport import DicomSource
from src.utils.logging_config import get_logger
from src.utils.nodes import NodeConfig

log = get_logger(__name__, "DESTINATIONS")

VALID_TRANSPORTS = ("rest", "stow-rs", "dimse")


class StorageCommitmentError(RuntimeError):
    """Raised when DICOM Storage Commitment fails or times out before confirmation."""


class StowRsRejectedError(RuntimeError):
    """Raised when a DICOMweb STOW-RS server reports failed SOP instances."""


async def forward_rest_instance(
    client: httpx.AsyncClient,
    source: DicomSource,
    instance_id: str,
    node_cfg: NodeConfig,
    timeout_s: float = 120.0,
) -> int:
    """Stream one DICOM instance to a regional node via Orthanc REST POST /instances.

    Returns total bytes transmitted.
    """
    bytes_sent = 0

    async with source.open_stream(client, instance_id) as body:

        async def _counting_stream():  # type: ignore[no-untyped-def]
            nonlocal bytes_sent
            async for chunk in body:
                bytes_sent += len(chunk)
                yield chunk

        post_resp = await client.post(
            f"{node_cfg['base']}/instances",
            content=_counting_stream(),
            headers={"Content-Type": "application/dicom"},
            auth=node_cfg["auth"],
            timeout=timeout_s,
        )
        post_resp.raise_for_status()
    return bytes_sent


async def forward_stow_rs_instance(
    client: httpx.AsyncClient,
    source: DicomSource,
    instance_id: str,
    node_cfg: NodeConfig,
    timeout_s: float = 120.0,
) -> int:
    """Transmit one DICOM instance via standard DICOMweb STOW-RS (POST /dicom-web/studies).

    Wraps the raw DICOM Part-10 payload in an RFC 2387 ``multipart/related`` envelope
    with ``type="application/dicom"`` and validates that the DICOMweb JSON response
    reports no entries in the Failed SOP Sequence (0008,1198).
    """
    chunks: list[bytes] = []
    async with source.open_stream(client, instance_id) as body:
        async for chunk in body:
            chunks.append(chunk)
    dicom_bytes = b"".join(chunks)

    boundary = f"diomede-{uuid.uuid4().hex}"
    multipart_body = (
        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode("ascii")
        + dicom_bytes
        + f"\r\n--{boundary}--\r\n".encode("ascii")
    )

    resp = await client.post(
        f"{node_cfg['base']}/dicom-web/studies",
        content=multipart_body,
        headers={
            "Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}',
            "Accept": "application/dicom+json",
        },
        auth=node_cfg["auth"],
        timeout=timeout_s,
    )
    resp.raise_for_status()

    # Validate DICOMweb STOW-RS response payload if JSON is returned.
    if resp.content:
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError:
            payload = {}
        failed_seq = payload.get("00081198", {}).get("Value", [])
        if failed_seq:
            raise StowRsRejectedError(
                f"STOW-RS rejected {len(failed_seq)} SOP instance(s) for {instance_id}"
            )

    return len(dicom_bytes)


async def forward_dimse_study(
    client: httpx.AsyncClient,
    edge_base: str,
    edge_auth: tuple[str, str],
    node_id: str,
    instance_ids: list[str],
    require_storage_commitment: bool = True,
    store_timeout_s: float = 180.0,
    sc_timeout_s: float = 30.0,
    sc_poll_interval_s: float = 0.25,
) -> bool:
    """Forward instances from Edge Orthanc to a cloud node via mutual-TLS DIMSE C-STORE.

    When ``require_storage_commitment`` is True, executes a DICOM Storage Commitment
    Push Model transaction (N-ACTION / N-EVENT-REPORT) via Edge Orthanc and waits for
    all instances to be durably committed on the cloud PACS before returning True.
    """
    base = edge_base.rstrip("/")
    store_resp = await client.post(
        f"{base}/modalities/{node_id}/store",
        json={"Resources": instance_ids, "Synchronous": True},
        auth=edge_auth,
        timeout=store_timeout_s,
    )
    store_resp.raise_for_status()

    if not require_storage_commitment:
        return False

    sc_resp = await client.post(
        f"{base}/modalities/{node_id}/storage-commitment",
        json={"Resources": instance_ids},
        auth=edge_auth,
        timeout=30.0,
    )
    sc_resp.raise_for_status()
    sc_data: dict[str, Any] = sc_resp.json()
    tx_id = sc_data.get("ID")
    if not tx_id:
        raise StorageCommitmentError(
            f"Storage commitment response from edge missing transaction ID for node {node_id}"
        )

    deadline = time.monotonic() + sc_timeout_s
    while time.monotonic() < deadline:
        poll_resp = await client.get(
            f"{base}/storage-commitment/{tx_id}",
            auth=edge_auth,
            timeout=10.0,
        )
        poll_resp.raise_for_status()
        report: dict[str, Any] = poll_resp.json()
        status_val = report.get("Status")
        if status_val == "Success":
            failures = report.get("Failures") or []
            if failures:
                raise StorageCommitmentError(
                    f"Storage commitment on {node_id} reported {len(failures)} failed instance(s)"
                )
            return True
        if status_val == "Failure":
            raise StorageCommitmentError(
                f"Storage commitment transaction {tx_id} failed on node {node_id}"
            )
        await asyncio.sleep(sc_poll_interval_s)

    raise StorageCommitmentError(
        f"Storage commitment transaction {tx_id} timed out after {sc_timeout_s:.1f}s on {node_id}"
    )


async def probe_dimse_echo(
    client: httpx.AsyncClient,
    edge_base: str,
    edge_auth: tuple[str, str],
    node_id: str,
    timeout_s: float = 10.0,
) -> float:
    """Measure DICOM-path RTT (ms) via mutual-TLS DIMSE C-ECHO from Edge Orthanc."""
    base = edge_base.rstrip("/")
    t0 = time.monotonic()
    resp = await client.post(
        f"{base}/modalities/{node_id}/echo",
        auth=edge_auth,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    return round((time.monotonic() - t0) * 1000.0, 1)
