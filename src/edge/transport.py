"""
edge/transport.py – Abstract base class for reading DICOM instances from the edge buffer.

Each concrete protocol implementation of the DicomSource interface
lives in its own module (orthanc_source.py, dimse_source.py, etc.) so support
for a new edge-side transport can be added without modifying this file or any
existing implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager

import httpx


class DicomSource(ABC):
    """Protocol-agnostic interface for reading DICOM instances out of the edge buffer.

    Subclass this to support a new edge-side transport (Orthanc REST, DIMSE
    C-STORE listener, DICOMweb STOW-RS, etc.) without changing the forwarding loop.
    """

    @abstractmethod
    async def poll_new(self, client: httpx.AsyncClient) -> dict[str, list[str]]:
        """Return the IDs of new instances ready to be routed, grouped by study.

        Keys are opaque study IDs (never DICOM UIDs or other PHI); the forwarder
        sends every instance of one study to the same node.
        """
        ...

    @abstractmethod
    def open_stream(
        self, client: httpx.AsyncClient, instance_id: str
    ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """Open a streaming read of instance_id's raw DICOM bytes.

        Returns an async context manager yielding an async byte iterator so the
        caller can pipe it straight to the destination without ever holding the
        whole file in memory.
        """
        ...

    @abstractmethod
    async def acknowledge(self, client: httpx.AsyncClient, instance_id: str) -> None:
        """Acknowledge successful routing (e.g. remove from edge buffer)."""
        ...
