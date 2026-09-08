"""Canonical fixed-width Windows file metadata layouts; no DLL loading."""

import ctypes


class FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("file_attributes", ctypes.c_uint32),
    ]


class FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("allocation_size", ctypes.c_int64),
        ("end_of_file", ctypes.c_int64),
        ("number_of_links", ctypes.c_uint32),
        ("delete_pending", ctypes.c_ubyte), ("directory", ctypes.c_ubyte),
    ]


class FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_uint64), ("file_id", ctypes.c_ubyte * 16),
    ]
