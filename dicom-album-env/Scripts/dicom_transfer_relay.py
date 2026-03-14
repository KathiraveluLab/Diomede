import os
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage
)

DEST_IP = os.environ.get("DEST_IP", "127.0.0.1")      # Placeholder for destination node IP
DEST_PORT = int(os.environ.get("DEST_PORT", 11112))    # Placeholder for destination node port
DEST_AE = os.environ.get("DEST_AE", "DEST_NODE_AE")     # Placeholder for destination AE Title

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")  # Placeholder for local host to listen on
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", 11111))     # Placeholder for local listening port

def forward_dataset(dataset):

    ae = AE()
    
    ae.add_requested_context(CTImageStorage)
    ae.add_requested_context(MRImageStorage)
    ae.add_requested_context(SecondaryCaptureImageStorage)

    assoc = ae.associate(DEST_IP, DEST_PORT, ae_title=DEST_AE)

    if assoc.is_established:
        print(f"Forwarding dataset: StudyUID={dataset.StudyInstanceUID}")
        try:
            status = assoc.send_c_store(dataset)
        finally:
            assoc.release()
        return status
    else:
        raise ConnectionError(f"Could not associate with destination AE: {DEST_AE}")

def handle_store(event):

    dataset = event.dataset
    dataset.file_meta = event.file_meta

    try:
        status = forward_dataset(dataset)
        return status.Status
    except ConnectionError as e:
        print("Relay error:", e)
        return 0xA700

handlers = [(evt.EVT_C_STORE, handle_store)]

ae = AE()

ae.add_supported_context(CTImageStorage)
ae.add_supported_context(MRImageStorage)
ae.add_supported_context(SecondaryCaptureImageStorage)

print(f"Starting DICOM Transfer Relay on {LISTEN_HOST}:{LISTEN_PORT}")
ae.start_server((LISTEN_HOST, LISTEN_PORT), evt_handlers=handlers)