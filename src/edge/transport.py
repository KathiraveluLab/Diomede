"""
edge/transport.py – Abstract base class for reading DICOM instances from the edge buffer.

Each concrete protocol implementation of the DicomSource interface
lives in its own module (orthanc_source.py, dimse_source.py, etc.) so support
for a new edge-side transport can be added without modifying this file or any
existing implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx


class DicomSource(ABC):
    """Protocol-agnostic interface for reading DICOM instances out of the edge buffer.

    Subclass this to support a new edge-side transport (Orthanc REST, DIMSE
    C-STORE listener, DICOMweb STOW-RS, etc.) without changing the forwarding loop.
    """

    @abstractmethod
    async def poll_new(self, client: httpx.AsyncClient) -> list[str]:
        """Return the IDs of new instances ready to be routed."""
        ...

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, instance_id: str) -> bytes:
        """Fetch the raw DICOM bytes for instance_id."""
        ...

    @abstractmethod
    async def acknowledge(self, client: httpx.AsyncClient, instance_id: str) -> None:
        """Acknowledge successful routing (e.g. remove from edge buffer)."""
        ...
