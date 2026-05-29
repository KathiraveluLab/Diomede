"""Unit tests for src/simulator/generate_dicom.py"""

import math

import pytest
from pydicom import uid as dcm_uid

from src.simulator.generate_dicom import (
    _RTT_SERIES_UID,
    _RTT_SOP_UID,
    _RTT_STUDY_UID,
    make_ct_8x8,
    make_sized,
)

pytestmark = pytest.mark.unit


class TestMakeCt8x8:
    def test_dimensions(self):
        ds = make_ct_8x8()
        assert ds.Rows == 8
        assert ds.Columns == 8

    def test_pixel_data_all_zeros(self):
        ds = make_ct_8x8()
        assert ds.PixelData == bytes(64)

    def test_fixed_uids(self):
        ds = make_ct_8x8()
        assert ds.SOPInstanceUID == _RTT_SOP_UID
        assert ds.StudyInstanceUID == _RTT_STUDY_UID
        assert ds.SeriesInstanceUID == _RTT_SERIES_UID

    def test_patient_tags(self):
        ds = make_ct_8x8()
        assert str(ds.PatientName) == "TEST^SIMULATOR"
        assert ds.PatientID == "SIM001"

    def test_pixel_format_tags(self):
        ds = make_ct_8x8()
        assert ds.SamplesPerPixel == 1
        assert ds.PhotometricInterpretation == "MONOCHROME2"
        assert ds.BitsAllocated == 8
        assert ds.BitsStored == 8
        assert ds.HighBit == 7
        assert ds.PixelRepresentation == 0

    def test_sop_class_uid(self):
        ds = make_ct_8x8()
        assert ds.SOPClassUID == dcm_uid.SecondaryCaptureImageStorage

    def test_transfer_syntax(self):
        ds = make_ct_8x8()
        assert ds.file_meta.TransferSyntaxUID == dcm_uid.ExplicitVRLittleEndian

    def test_returns_same_uids_on_repeated_calls(self):
        # Fixed UIDs — deduplication by design
        ds1 = make_ct_8x8()
        ds2 = make_ct_8x8()
        assert ds1.SOPInstanceUID == ds2.SOPInstanceUID


class TestMakeSized:
    @pytest.mark.parametrize("kb", [1, 4, 16, 64, 256])
    def test_pixel_data_at_least_requested_size(self, kb):
        ds = make_sized(kb)
        assert len(ds.PixelData) >= kb * 1024

    @pytest.mark.parametrize("kb", [1, 4, 16, 64])
    def test_rows_columns_match_ceil_sqrt(self, kb):
        ds = make_sized(kb)
        expected_side = math.ceil(math.sqrt(kb * 1024))
        assert ds.Rows == expected_side
        assert ds.Columns == expected_side

    def test_pixel_data_matches_dimensions(self):
        ds = make_sized(4)
        assert len(ds.PixelData) == ds.Rows * ds.Columns

    def test_unique_uids_per_call(self):
        ds1 = make_sized(1)
        ds2 = make_sized(1)
        assert ds1.SOPInstanceUID != ds2.SOPInstanceUID
        assert ds1.StudyInstanceUID != ds2.StudyInstanceUID
        assert ds1.SeriesInstanceUID != ds2.SeriesInstanceUID
