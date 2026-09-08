from __future__ import annotations

import ctypes

from aiworkhub.windows_file_structures import (
    FILETIME,
    FILE_BASIC_INFO,
    FILE_ID_INFO,
    FILE_STANDARD_INFO,
)


def test_windows_file_metadata_layout_sizes_and_alignment() -> None:
    assert ctypes.sizeof(FILETIME) == 8
    assert ctypes.alignment(FILETIME) == 4
    assert ctypes.sizeof(FILE_BASIC_INFO) == 40
    assert ctypes.alignment(FILE_BASIC_INFO) == 8
    assert ctypes.sizeof(FILE_STANDARD_INFO) == 24
    assert ctypes.alignment(FILE_STANDARD_INFO) == 8
    assert ctypes.sizeof(FILE_ID_INFO) == 24
    assert ctypes.alignment(FILE_ID_INFO) == 8


def test_windows_file_metadata_offsets_and_fixed_width_types() -> None:
    assert FILETIME.low.offset == 0
    assert FILETIME.high.offset == 4
    assert FILE_BASIC_INFO.file_attributes.offset == 32
    assert FILE_STANDARD_INFO.number_of_links.offset == 16
    assert FILE_STANDARD_INFO.delete_pending.offset == 20
    assert FILE_STANDARD_INFO.directory.offset == 21
    assert FILE_ID_INFO.file_id.offset == 8
    fields = dict(FILE_STANDARD_INFO._fields_)
    assert ctypes.sizeof(fields["delete_pending"]) == 1
    assert ctypes.sizeof(fields["directory"]) == 1


def test_file_id_preserves_all_128_little_endian_bits() -> None:
    value = (1 << 127) | 0x0102030405060708090A0B0C0D0E0F
    info = FILE_ID_INFO(volume_serial_number=7)
    encoded = value.to_bytes(16, "little")
    info.file_id[:] = encoded
    assert bytes(info.file_id) == encoded
    assert int.from_bytes(bytes(info.file_id), "little") == value


def test_filetime_preserves_low_and_high_halves() -> None:
    value = FILETIME(low=0x89ABCDEF, high=0xFEDCBA98)
    assert (int(value.high) << 32) | int(value.low) == 0xFEDCBA9889ABCDEF
