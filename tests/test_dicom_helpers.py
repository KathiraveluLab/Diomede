import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset

from Diomedex.utils.dicom_helpers import extract_basic_metadata


def test_extract_basic_metadata_returns_expected_fields(tmp_path):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = pydicom.uid.generate_uid()
    file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    ds = FileDataset(str(tmp_path / "test.dcm"), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientID = "P123"
    ds.StudyDate = "20260101"
    ds.Modality = "CT"
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.save_as(str(tmp_path / "test.dcm"))

    metadata = extract_basic_metadata(tmp_path / "test.dcm")

    assert metadata == {
        'PatientID': "P123",
        'StudyDate': "20260101",
        'Modality': "CT",
        'SeriesInstanceUID': ds.SeriesInstanceUID,
    }

    missing_ds = FileDataset(str(tmp_path / "missing.dcm"), {}, file_meta=file_meta, preamble=b"\0" * 128)
    missing_ds.PatientID = "P123"
    missing_ds.save_as(str(tmp_path / "missing.dcm"))
    missing_metadata = extract_basic_metadata(tmp_path / "missing.dcm")
    assert missing_metadata['StudyDate'] is None
