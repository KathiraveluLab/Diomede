import os
import logging
import time
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage
)
import threading
lock = threading.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dicom-relay")

total_received = 0
total_forwarded = 0
SUPPORTED_CONTEXTS = [
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage,
]

DEST_IP = os.environ.get("DEST_IP", "127.0.0.1")

def get_int_env(var, default):
    try:
        return int(os.environ.get(var, default))
    except ValueError:
        raise ValueError(f"Invalid value for {var}, must be integer")

DEST_PORT = get_int_env("DEST_PORT", 11112)
DEST_AE = os.environ.get("DEST_AE", "DEST_NODE_AE")

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = get_int_env("LISTEN_PORT", 11111)

MAX_PDU = get_int_env("MAX_PDU", 16384)
RETRY_COUNT = get_int_env("RETRY_COUNT", 3)
RETRY_BACKOFF_BASE = get_int_env("RETRY_BACKOFF_BASE", 2)

def forward_dataset(dataset, retries=RETRY_COUNT):
    """
    This will forward a DICOM dataset to destination AE with retry support.
    """

    client_ae = AE()
    for context in SUPPORTED_CONTEXTS:
        client_ae.add_requested_context(context)

    for attempt in range(retries):
        assoc = client_ae.associate(DEST_IP, DEST_PORT, ae_title=DEST_AE, max_pdu=MAX_PDU)

        if assoc.is_established:
            try:
                study_uid = getattr(dataset, "StudyInstanceUID", "UNKNOWN")
                logger.info(f"Forwarding dataset: StudyUID={study_uid}")

                status = assoc.send_c_store(dataset)
                return status

            except Exception as e:
                logger.error(f"Send failed: {e}")

            finally:
                assoc.release()   
        else:
            logger.error("Association failed")

        time.sleep(RETRY_BACKOFF_BASE** attempt)

    raise ConnectionError(f"Failed after {retries} retries")

def handle_store(event):
    """
    This will handle incoming C-STORE request and forward dataset.
    """
    with lock:
        global total_received
        total_received += 1

    dataset = event.dataset
    dataset.file_meta = event.file_meta

    try:
        status = forward_dataset(dataset)

        if status.Status == 0x0000:
            with lock:
                global total_forwarded
                total_forwarded += 1
                logger.info(f"Forwarded {total_forwarded}/{total_received}")
        else:
            logger.warning(f"Forwarding failed with status: {status.Status:#06x}")

        return status.Status
    except ConnectionError as e:
        logger.error(f"Relay error: {e}")
        return 0xA700

handlers = [(evt.EVT_C_STORE, handle_store)]

ae = AE()

# Supported (incoming)
for context in SUPPORTED_CONTEXTS:
    ae.add_supported_context(context)

if __name__ == "__main__":
    logger.info(f"Starting DICOM Transfer Relay on {LISTEN_HOST}:{LISTEN_PORT}")
    ae.start_server((LISTEN_HOST, LISTEN_PORT), evt_handlers=handlers)