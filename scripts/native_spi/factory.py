"""Inspect a merged ESP32-S3 factory image without an application ELF."""
from pathlib import Path
import hashlib
import struct

MAX_IMAGE = 32 * 1024 * 1024


def verify_image(data, offset, limit):
    if offset + 24 > limit or data[offset] != 0xe9:
        raise ValueError("ESP image header is missing")
    segments = data[offset + 1]
    if not 1 <= segments <= 16:
        raise ValueError("Invalid ESP segment count")
    if int.from_bytes(data[offset + 12:offset + 14], "little") != 9:
        raise ValueError("ESP32-S3 firmware is required")
    cursor = offset + 24
    checksum = 0xef
    for _ in range(segments):
        if cursor + 8 > limit:
            raise ValueError("Truncated ESP segment header")
        address, length = struct.unpack_from("<II", data, cursor)
        cursor += 8
        if length > MAX_IMAGE or cursor + length > limit:
            raise ValueError("Truncated or excessive ESP segment")
        for value in data[cursor:cursor + length]:
            checksum ^= value
        cursor += length
    checksum_offset = cursor | 15
    if checksum_offset >= limit or data[checksum_offset] != checksum:
        raise ValueError("ESP image checksum mismatch")
    end = checksum_offset + 1
    if data[offset + 23] not in (0, 1):
        raise ValueError("Invalid image hash flag")
    appended = bool(data[offset + 23])
    if appended:
        if end + 32 > limit or hashlib.sha256(data[offset:end]).digest() != data[end:end + 32]:
            raise ValueError("ESP image SHA-256 mismatch")
        end += 32
    return {"offset": offset, "end": end, "segments": segments, "appended_sha256_verified": appended}


def factory_info(path):
    with Path(path).open("rb") as source:
        data = source.read(MAX_IMAGE + 1)
    if not 0x10000 < len(data) <= MAX_IMAGE:
        raise ValueError("Factory image size outside bounds")
    boot = verify_image(data, 0, min(len(data), 0x8000))
    partitions = []
    app = None
    app_size = None
    table_start = 0x8000
    table_md5 = False
    labels = set()
    for offset in range(table_start, 0x9000, 32):
        row = data[offset:offset + 32]
        if row[:2] == b"\xff\xff":
            break
        if row[:2] == b"\xeb\xeb":
            if hashlib.md5(data[table_start:offset]).digest() != row[16:32]:
                raise ValueError("Partition-table MD5 mismatch")
            table_md5 = True
            break
        if row[:2] != b"\xaa\x50":
            raise ValueError("Valid merged factory partition table required")
        kind, subtype = row[2], row[3]
        position, size = struct.unpack_from("<II", row, 4)
        label = row[12:28].split(b"\x00")[0].decode("ascii")
        if not size or position < 0x9000 or position + size > MAX_IMAGE or label in labels:
            raise ValueError("Invalid or duplicate factory partition")
        for prior in partitions:
            if position < prior["offset"] + prior["size"] and prior["offset"] < position + size:
                raise ValueError("Overlapping factory partitions")
        labels.add(label)
        partitions.append({"name": label, "type": kind, "subtype": subtype, "offset": position, "size": size})
        if kind == 0 and subtype == 0x10:
            if app is not None:
                raise ValueError("Multiple primary OTA application partitions")
            app = position
            app_size = size
    if app is None or app + 288 > len(data):
        raise ValueError("Factory primary application partition is missing")
    application = verify_image(data, app, min(len(data), app + app_size))
    desc = data[app + 32:app + 288]
    if int.from_bytes(desc[:4], "little") != 0xabcd5432:
        raise ValueError("Application descriptor missing")
    text = lambda start, end: desc[start:end].split(b"\x00")[0].decode("utf8", errors="strict")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "chip": "esp32s3",
            "application_offset": app, "project": text(48, 80), "version": text(16, 48), "idf": text(112, 144),
            "partition_md5_verified": table_md5, "boot": boot, "application": application, "partitions": partitions}
