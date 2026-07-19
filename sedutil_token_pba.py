#!/usr/bin/env python3
"""
sedutil_token_pba.py - Windows-first sedutil PBA USB-token personalizer.

Requires Python 3.12+ and uses only the Python standard library.

Supported PBA shape:
  - raw GPT/UEFI sedutil PBA image, optionally gzip-compressed
  - first GPT partition is a FAT16-style EFI System Partition
  - ESP contains /EFI/boot/rootfs.cpio.xz
  - rootfs is a cpio newc archive containing /sbin/linuxpba

This tool never modifies the input image in place. It writes a new .img and .img.gz.
"""

from __future__ import annotations

import argparse
import binascii
import dataclasses
import getpass
import gzip
import hashlib
import lzma
import math
import os
import shutil
import stat
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

PY_MIN = (3, 12)

SHARE_LEN = 512
HEADER_LEN = 32
FILE_LEN = HEADER_LEN + SHARE_LEN
RECORD_HEADER_LEN = 32
MAX_PASSWORD = 256
FILE_MAGIC = b"SEDSHR1\0"
RECORD_MAGIC = b"SEDPWD1\0"
GZIP_MAGIC = b"\x1f\x8b"
SECTOR = 512
EFI_PARTITION_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
ROOTFS_PATH = "/EFI/boot/rootfs.cpio.xz"


class ToolError(RuntimeError):
    pass


def fail(msg: str) -> None:
    raise ToolError(msg)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_gzip_file(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(2) == GZIP_MAGIC


def read_input_image(path: Path) -> tuple[bytes, bool]:
    data = path.read_bytes()
    if data.startswith(GZIP_MAGIC):
        return gzip.decompress(data), True
    return data, False


def gzip_write_reproducible(path: Path, data: bytes, compresslevel: int = 6) -> None:
    # mtime=0 gives deterministic gzip output for same data/path-independent header.
    with path.open("wb") as f:
        with gzip.GzipFile(filename="", mode="wb", fileobj=f, compresslevel=compresslevel, mtime=0) as gz:
            gz.write(data)


def display_guid(raw16: bytes) -> str:
    return str(uuid.UUID(bytes_le=raw16))


def make_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_cpio_name(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("/"):
        name = name[1:]
    if name.startswith("./"):
        name = name[2:]
    return name or "."


def get_password_from_args(args: argparse.Namespace) -> str:
    sources = [bool(getattr(args, "password", None)), bool(getattr(args, "password_stdin", False))]
    if sum(sources) > 1:
        fail("Use only one password source: prompt, --password, or --password-stdin.")

    if getattr(args, "password", None):
        print("WARNING: --password can expose the password in shell history/process lists. Prefer prompt input.", file=sys.stderr)
        return args.password

    if getattr(args, "password_stdin", False):
        pw = sys.stdin.readline()
        if pw.endswith("\n"):
            pw = pw[:-1]
        if pw.endswith("\r"):
            pw = pw[:-1]
        return pw

    pw1 = getpass.getpass("Existing sedutil password: ")
    if not getattr(args, "no_confirm", False):
        pw2 = getpass.getpass("Confirm sedutil password: ")
        if pw1 != pw2:
            fail("Passwords did not match.")
    return pw1


def validate_password(pw: str) -> bytes:
    try:
        data = pw.encode("ascii")
    except UnicodeEncodeError:
        fail("Password must be printable US-ASCII for this v1 share format.")
    if not 1 <= len(data) <= MAX_PASSWORD:
        fail(f"Password must be 1..{MAX_PASSWORD} bytes.")
    for b in data:
        if b < 0x20 or b > 0x7E:
            fail("Password contains a character outside printable ASCII 0x20..0x7E.")
    return data


def create_share_files(password: bytes, out_dir: Path, force: bool = False, unlock_out: Optional[Path] = None) -> tuple[Path, Path]:
    """Write the two shares. With unlock_out (e.g. a token USB path), UNLOCK.BIN
    is written only there, so both shares never coexist in out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    machine_path = out_dir / "machine-share.bin"
    unlock_path = unlock_out if unlock_out is not None else out_dir / "UNLOCK.BIN"
    for p in (machine_path, unlock_path):
        if p.exists() and not force:
            fail(f"Refusing to overwrite existing file: {p} (use --force)")
    make_parent(unlock_path)

    machine_id = os.urandom(16)
    machine_payload = os.urandom(SHARE_LEN)

    record = bytearray(SHARE_LEN)
    record[0:8] = RECORD_MAGIC
    record[8] = 1
    record[10:12] = struct.pack("<H", len(password))
    record[RECORD_HEADER_LEN:RECORD_HEADER_LEN + len(password)] = password

    token_payload = bytes(a ^ b for a, b in zip(machine_payload, record))

    header = bytearray(HEADER_LEN)
    header[0:8] = FILE_MAGIC
    header[8] = 1
    header[16:32] = machine_id

    machine_bytes = bytes(header) + machine_payload
    unlock_bytes = bytes(header) + token_payload
    machine_path.write_bytes(machine_bytes)
    unlock_path.write_bytes(unlock_bytes)
    if machine_path.read_bytes() != machine_bytes or unlock_path.read_bytes() != unlock_bytes:
        fail("Written share files did not verify byte-for-byte.")

    # Best-effort local permission tightening on POSIX. On Windows this is harmless/no-op-ish.
    try:
        os.chmod(machine_path, 0o600)
        os.chmod(unlock_path, 0o600)
    except OSError:
        pass

    # Reduce lifetime of raw password/record in this process as much as Python reasonably allows.
    for i in range(len(record)):
        record[i] = 0

    return machine_path, unlock_path


def read_share(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != FILE_LEN:
        fail(f"{path} must be exactly {FILE_LEN} bytes; got {len(data)}.")
    if data[:8] != FILE_MAGIC or data[8] != 1:
        fail(f"{path} is not a v1 SEDSHR1 share file.")
    return data


def verify_share_pair(machine_path: Path, unlock_path: Path) -> dict[str, object]:
    m = read_share(machine_path)
    u = read_share(unlock_path)
    if m[16:32] != u[16:32]:
        fail("machine-share.bin and UNLOCK.BIN machine IDs do not match.")
    record = bytes(m[HEADER_LEN + i] ^ u[HEADER_LEN + i] for i in range(SHARE_LEN))
    if record[:8] != RECORD_MAGIC or record[8] != 1:
        fail("Shares combine, but reconstructed record header is invalid.")
    pwlen = struct.unpack("<H", record[10:12])[0]
    if pwlen == 0 or pwlen > MAX_PASSWORD or RECORD_HEADER_LEN + pwlen > SHARE_LEN:
        fail("Shares combine, but reconstructed password length is invalid.")
    printable = all(0x20 <= b <= 0x7E for b in record[RECORD_HEADER_LEN:RECORD_HEADER_LEN + pwlen])
    if not printable:
        fail("Shares combine, but reconstructed password contains non-printable bytes.")
    return {
        "machine_id_hex": m[16:32].hex(),
        "password_length": pwlen,
        "machine_sha256": sha256_bytes(m),
        "unlock_sha256": sha256_bytes(u),
    }


@dataclasses.dataclass
class GptInfo:
    disk_guid: str
    first_part_type_guid: str
    first_part_unique_guid: str
    first_lba: int
    last_lba: int
    part_name: str


def parse_gpt(raw: bytes) -> GptInfo:
    if len(raw) < 34 * SECTOR:
        fail("Image is too small to contain a GPT.")
    hdr = raw[SECTOR:SECTOR + 92]
    if hdr[:8] != b"EFI PART":
        fail("GPT header not found at LBA 1.")
    part_lba = struct.unpack("<Q", hdr[72:80])[0]
    num_entries = struct.unpack("<I", hdr[80:84])[0]
    entry_size = struct.unpack("<I", hdr[84:88])[0]
    if entry_size < 128 or num_entries == 0:
        fail("Unsupported GPT partition-entry layout.")
    off = part_lba * SECTOR
    if off + entry_size > len(raw):
        fail("GPT partition array is outside image.")
    e = raw[off:off + entry_size]
    if e[:16] == b"\0" * 16:
        fail("First GPT partition is empty.")
    first_lba = struct.unpack("<Q", e[32:40])[0]
    last_lba = struct.unpack("<Q", e[40:48])[0]
    if first_lba <= 0 or last_lba < first_lba or (last_lba + 1) * SECTOR > len(raw):
        fail("First GPT partition extents are invalid for this image.")
    try:
        name = e[56:128].decode("utf-16le").rstrip("\x00")
    except UnicodeDecodeError:
        name = ""
    return GptInfo(
        disk_guid=display_guid(hdr[56:72]),
        first_part_type_guid=display_guid(e[0:16]),
        first_part_unique_guid=display_guid(e[16:32]),
        first_lba=first_lba,
        last_lba=last_lba,
        part_name=name,
    )


class Fat16Image:
    """Narrow FAT16 reader/replacer for the sedutil UEFI PBA ESP."""

    def __init__(self, image: bytearray, part_off: int):
        self.image = image
        self.part_off = part_off
        b = bytes(image[part_off:part_off + SECTOR])
        if b[510:512] != b"\x55\xaa":
            fail("FAT boot-sector signature not found at EFI partition offset.")
        self.bps = struct.unpack("<H", b[11:13])[0]
        self.spc = b[13]
        self.rsv = struct.unpack("<H", b[14:16])[0]
        self.nfats = b[16]
        self.root_entries = struct.unpack("<H", b[17:19])[0]
        self.totsec = struct.unpack("<H", b[19:21])[0] or struct.unpack("<I", b[32:36])[0]
        self.spf = struct.unpack("<H", b[22:24])[0]
        if self.bps != 512 or self.spc == 0 or self.spf == 0 or self.root_entries == 0:
            fail("Only FAT16-style PBA ESP images are supported by this tool.")
        self.fat_off = part_off + self.rsv * self.bps
        self.root_off = part_off + (self.rsv + self.nfats * self.spf) * self.bps
        self.root_size = ((self.root_entries * 32 + self.bps - 1) // self.bps) * self.bps
        self.data_off = self.root_off + self.root_size
        self.cluster_size = self.spc * self.bps
        self.data_secs = self.totsec - (self.rsv + self.nfats * self.spf + self.root_size // self.bps)
        self.num_clusters = self.data_secs // self.spc
        # Fewer than 4085 clusters means the volume is FAT12, whose packed
        # 1.5-byte FAT entries this 2-byte reader would silently misparse.
        if self.num_clusters < 4085:
            fail("ESP has fewer than 4085 clusters (FAT12); only FAT16 PBA ESP images are supported.")

    def clus_off(self, c: int) -> int:
        return self.data_off + (c - 2) * self.cluster_size

    def get_next(self, c: int) -> int:
        off = self.fat_off + c * 2
        return struct.unpack("<H", self.image[off:off + 2])[0]

    def set_next_all(self, c: int, val: int) -> None:
        for i in range(self.nfats):
            off = self.fat_off + i * self.spf * self.bps + c * 2
            self.image[off:off + 2] = struct.pack("<H", val)

    def chain(self, start: int) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        c = start
        while c >= 2 and c < 0xFFF8 and c not in seen:
            out.append(c)
            seen.add(c)
            c = self.get_next(c)
        return out

    def read_dir_raw(self, start: Optional[int] = None) -> bytes:
        if start is None:
            return bytes(self.image[self.root_off:self.root_off + self.root_size])
        chunks = []
        for c in self.chain(start):
            off = self.clus_off(c)
            chunks.append(bytes(self.image[off:off + self.cluster_size]))
        return b"".join(chunks)

    def parse_dir(self, start: Optional[int] = None) -> list[dict[str, object]]:
        raw = self.read_dir_raw(start)
        entries: list[dict[str, object]] = []
        lfns: list[str] = []
        for off in range(0, len(raw), 32):
            e = raw[off:off + 32]
            if len(e) < 32 or e[0] == 0x00:
                break
            if e[0] == 0xE5:
                lfns = []
                continue
            attr = e[11]
            if attr == 0x0F:
                chars = e[1:11] + e[14:26] + e[28:32]
                try:
                    s = chars.decode("utf-16le").split("\x00")[0].replace("\uffff", "")
                except UnicodeDecodeError:
                    s = ""
                lfns.insert(0, s)
                continue
            name = e[:8].decode("ascii", "replace").rstrip()
            ext = e[8:11].decode("ascii", "replace").rstrip()
            short = name + (("." + ext) if ext else "")
            lfn = "".join(lfns) if lfns else short
            lfns = []
            cl = (struct.unpack("<H", e[20:22])[0] << 16) | struct.unpack("<H", e[26:28])[0]
            size = struct.unpack("<I", e[28:32])[0]
            entries.append({"name": lfn, "short": short, "attr": attr, "cluster": cl, "size": size, "dir_off": off, "dir_start": start})
        return entries

    def find_path(self, path: str) -> dict[str, object]:
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        start: Optional[int] = None
        cur: Optional[dict[str, object]] = None
        for idx, p in enumerate(parts):
            matches = [e for e in self.parse_dir(start) if str(e["name"]).lower() == p.lower() or str(e["short"]).lower() == p.lower()]
            if not matches:
                fail(f"FAT path not found in PBA image: {path}")
            cur = matches[0]
            if idx != len(parts) - 1:
                start = int(cur["cluster"])
        assert cur is not None
        return cur

    def read_file(self, path: str) -> bytes:
        e = self.find_path(path)
        data = bytearray()
        for c in self.chain(int(e["cluster"])):
            off = self.clus_off(c)
            data += self.image[off:off + self.cluster_size]
        return bytes(data[:int(e["size"])])

    def read_fat(self) -> bytes:
        return bytes(self.image[self.fat_off:self.fat_off + self.spf * self.bps])

    def replace_file(self, path: str, data: bytes) -> None:
        e = self.find_path(path)
        chain = self.chain(int(e["cluster"]))
        need = math.ceil(len(data) / self.cluster_size) or 1
        if need > len(chain):
            fat = self.read_fat()
            used: set[int] = set()
            for c in range(2, self.num_clusters + 2):
                val = struct.unpack("<H", fat[c * 2:c * 2 + 2])[0]
                if val != 0:
                    used.add(c)
            free = [c for c in range(2, self.num_clusters + 2) if c not in used]
            add = free[:need - len(chain)]
            if len(add) < need - len(chain):
                fail("Not enough free FAT clusters to replace rootfs.cpio.xz in this PBA image.")
            chain += add
        for i, c in enumerate(chain):
            if i < need:
                val = chain[i + 1] if i + 1 < need else 0xFFFF
            else:
                val = 0
            self.set_next_all(c, val)
        used_chain = chain[:need]
        padded = data + b"\0" * (len(used_chain) * self.cluster_size - len(data))
        for idx, c in enumerate(used_chain):
            off = self.clus_off(c)
            self.image[off:off + self.cluster_size] = padded[idx * self.cluster_size:(idx + 1) * self.cluster_size]
        if e["dir_start"] is None:
            dir_off = self.root_off + int(e["dir_off"])
        else:
            dir_off = self.clus_off(int(e["dir_start"])) + int(e["dir_off"])
        self.image[dir_off + 28:dir_off + 32] = struct.pack("<I", len(data))


@dataclasses.dataclass
class CpioEntry:
    name: str
    fields: list[int]
    data: bytes

    @property
    def mode(self) -> int:
        return self.fields[1]

    @mode.setter
    def mode(self, value: int) -> None:
        self.fields[1] = value

    @property
    def filesize(self) -> int:
        return self.fields[6]

    @filesize.setter
    def filesize(self, value: int) -> None:
        self.fields[6] = value

    @property
    def namesize(self) -> int:
        return self.fields[11]

    @namesize.setter
    def namesize(self, value: int) -> None:
        self.fields[11] = value


class CpioNewc:
    def __init__(self, entries: list[CpioEntry]):
        self.entries = entries

    @staticmethod
    def _align4(pos: int) -> int:
        return (pos + 3) & ~3

    @classmethod
    def read(cls, data: bytes) -> "CpioNewc":
        entries: list[CpioEntry] = []
        pos = 0
        while True:
            if pos + 110 > len(data):
                fail("Truncated cpio newc archive.")
            hdr = data[pos:pos + 110]
            pos += 110
            if hdr[:6] != b"070701":
                fail(f"Unsupported cpio archive: expected newc magic at offset {pos - 110}.")
            fields = []
            for i in range(13):
                raw = hdr[6 + i * 8:6 + (i + 1) * 8]
                try:
                    fields.append(int(raw.decode("ascii"), 16))
                except ValueError:
                    fail("Invalid hex field in cpio header.")
            namesize = fields[11]
            filesize = fields[6]
            if namesize <= 0 or pos + namesize > len(data):
                fail("Invalid cpio entry name size.")
            name_raw = data[pos:pos + namesize]
            pos += namesize
            pos = cls._align4(pos)
            name = name_raw[:-1].decode("utf-8", "surrogateescape") if name_raw.endswith(b"\0") else name_raw.decode("utf-8", "surrogateescape")
            if pos + filesize > len(data):
                fail(f"Truncated cpio data for {name!r}.")
            payload = data[pos:pos + filesize]
            pos += filesize
            pos = cls._align4(pos)
            entries.append(CpioEntry(name=name, fields=fields, data=payload))
            if name == "TRAILER!!!":
                break
        return cls(entries)

    def find(self, name: str) -> Optional[CpioEntry]:
        want = normalize_cpio_name(name)
        for e in self.entries:
            if normalize_cpio_name(e.name) == want:
                return e
        return None

    def has(self, name: str) -> bool:
        return self.find(name) is not None

    def max_inode(self) -> int:
        if not self.entries:
            return 1000
        return max(e.fields[0] for e in self.entries)

    def _before_trailer_index(self) -> int:
        for i, e in enumerate(self.entries):
            if e.name == "TRAILER!!!":
                return i
        fail("cpio archive has no TRAILER!!! entry.")

    def upsert_file(self, name: str, data: bytes, mode: int) -> None:
        norm = normalize_cpio_name(name)
        existing = self.find(norm)
        if existing:
            existing.data = data
            existing.mode = mode
            existing.filesize = len(data)
            existing.namesize = len(existing.name.encode("utf-8", "surrogateescape")) + 1
            existing.fields[5] = int(time.time())
            return
        fields = [0] * 13
        fields[0] = self.max_inode() + 1
        fields[1] = mode
        fields[2] = 0
        fields[3] = 0
        fields[4] = 1
        fields[5] = int(time.time())
        fields[6] = len(data)
        fields[11] = len(norm.encode("utf-8")) + 1
        self.entries.insert(self._before_trailer_index(), CpioEntry(norm, fields, data))

    def ensure_dir(self, name: str, mode: int = stat.S_IFDIR | 0o755) -> None:
        norm = normalize_cpio_name(name)
        existing = self.find(norm)
        if existing:
            existing.mode = mode
            return
        fields = [0] * 13
        fields[0] = self.max_inode() + 1
        fields[1] = mode
        fields[2] = 0
        fields[3] = 0
        fields[4] = 2
        fields[5] = int(time.time())
        fields[6] = 0
        fields[11] = len(norm.encode("utf-8")) + 1
        self.entries.insert(self._before_trailer_index(), CpioEntry(norm, fields, b""))

    @staticmethod
    def _write_entry(e: CpioEntry) -> bytes:
        name_bytes = e.name.encode("utf-8", "surrogateescape") + b"\0"
        fields = list(e.fields)
        fields[6] = len(e.data)
        fields[11] = len(name_bytes)
        header = b"070701" + b"".join(f"{v & 0xFFFFFFFF:08x}".encode("ascii") for v in fields)
        out = bytearray(header)
        out += name_bytes
        while len(out) % 4:
            out.append(0)
        out += e.data
        while len(out) % 4:
            out.append(0)
        return bytes(out)

    def to_bytes(self) -> bytes:
        return b"".join(self._write_entry(e) for e in self.entries)


def output_paths(output: Path) -> tuple[Path, Path]:
    s = str(output)
    if s.lower().endswith(".img.gz"):
        raw = Path(s[:-3])
        gz = output
    elif s.lower().endswith(".img"):
        raw = output
        gz = Path(s + ".gz")
    else:
        raw = Path(s + ".img")
        gz = Path(s + ".img.gz")
    return raw, gz


def transform_image(input_path: Path, machine_share_path: Path, sedtoken_path: Path, script_path: Path, output: Path, force: bool = False) -> tuple[Path, Path, Path, dict[str, object]]:
    raw_out, gz_out = output_paths(output)
    report_path = raw_out.with_suffix(raw_out.suffix + ".verify.txt")
    for p in (raw_out, gz_out, report_path):
        if p.exists() and not force:
            fail(f"Refusing to overwrite existing file: {p} (use --force)")
    for p in (input_path, machine_share_path, sedtoken_path, script_path):
        if not p.exists():
            fail(f"Required input not found: {p}")
    machine_share = read_share(machine_share_path)
    sedtoken = sedtoken_path.read_bytes()
    script = script_path.read_bytes()
    if b"sedutil-password.in" in script:
        fail("Selected S99PBA.sh appears to use the old temporary password file path.")

    input_raw, input_was_gzip = read_input_image(input_path)
    before_gpt = parse_gpt(input_raw)
    image = bytearray(input_raw)
    part_off = before_gpt.first_lba * SECTOR
    fs = Fat16Image(image, part_off)
    rootfs_xz = fs.read_file(ROOTFS_PATH)
    try:
        rootfs_cpio = lzma.decompress(rootfs_xz)
    except lzma.LZMAError as ex:
        fail(f"Could not decompress rootfs.cpio.xz: {ex}")
    archive = CpioNewc.read(rootfs_cpio)
    if not archive.has("sbin/linuxpba"):
        fail("Input PBA rootfs does not contain /sbin/linuxpba; refusing to transform.")

    archive.ensure_dir("etc/sedutil")
    archive.upsert_file("sbin/sedtoken", sedtoken, stat.S_IFREG | 0o755)
    archive.upsert_file("etc/init.d/S99PBA.sh", script, stat.S_IFREG | 0o755)
    archive.upsert_file("etc/sedutil/machine-share.bin", machine_share, stat.S_IFREG | 0o600)

    new_cpio = archive.to_bytes()
    new_xz = lzma.compress(new_cpio, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32, preset=0)
    fs.replace_file(ROOTFS_PATH, new_xz)
    output_raw = bytes(image)
    after_gpt = parse_gpt(output_raw)

    make_parent(raw_out)
    raw_out.write_bytes(output_raw)
    gzip_write_reproducible(gz_out, output_raw)

    report = {
        "input": str(input_path),
        "input_was_gzip": input_was_gzip,
        "output_raw": str(raw_out),
        "output_gz": str(gz_out),
        "input_size": len(input_raw),
        "output_size": len(output_raw),
        "input_disk_guid": before_gpt.disk_guid,
        "output_disk_guid": after_gpt.disk_guid,
        "input_partition_guid": before_gpt.first_part_unique_guid,
        "output_partition_guid": after_gpt.first_part_unique_guid,
        "rootfs_xz_old_size": len(rootfs_xz),
        "rootfs_xz_new_size": len(new_xz),
        "raw_sha256": sha256_file(raw_out),
        "gz_sha256": sha256_file(gz_out),
        "machine_share_sha256": sha256_bytes(machine_share),
        "sedtoken_sha256": sha256_bytes(sedtoken),
        "script_sha256": sha256_bytes(script),
        "guid_preserved": before_gpt.disk_guid == after_gpt.disk_guid and before_gpt.first_part_unique_guid == after_gpt.first_part_unique_guid,
        "no_temp_password_path_in_script": b"sedutil-password.in" not in script,
    }
    write_report(report_path, report)
    return raw_out, gz_out, report_path, report


def inspect_image(image_path: Path, machine_share_path: Optional[Path] = None) -> dict[str, object]:
    raw, was_gzip = read_input_image(image_path)
    gpt = parse_gpt(raw)
    fs = Fat16Image(bytearray(raw), gpt.first_lba * SECTOR)
    rootfs_xz = fs.read_file(ROOTFS_PATH)
    try:
        rootfs_cpio = lzma.decompress(rootfs_xz)
    except lzma.LZMAError as ex:
        fail(f"Could not decompress rootfs.cpio.xz: {ex}")
    archive = CpioNewc.read(rootfs_cpio)
    result: dict[str, object] = {
        "image": str(image_path),
        "was_gzip": was_gzip,
        "raw_size": len(raw),
        "disk_guid": gpt.disk_guid,
        "partition_type_guid": gpt.first_part_type_guid,
        "partition_guid": gpt.first_part_unique_guid,
        "partition_first_lba": gpt.first_lba,
        "partition_last_lba": gpt.last_lba,
        "partition_name": gpt.part_name,
        "found_rootfs": True,
        "rootfs_xz_size": len(rootfs_xz),
        "has_linuxpba": archive.has("sbin/linuxpba"),
        "has_sedtoken": archive.has("sbin/sedtoken"),
        "has_machine_share": archive.has("etc/sedutil/machine-share.bin"),
        "has_s99": archive.has("etc/init.d/S99PBA.sh"),
        "raw_sha256": sha256_bytes(raw),
    }
    ms = archive.find("etc/sedutil/machine-share.bin")
    if ms:
        result["embedded_machine_share_size"] = len(ms.data)
        result["embedded_machine_share_sha256"] = sha256_bytes(ms.data)
        try:
            read_share_bytes = ms.data
            result["embedded_machine_share_valid_header"] = len(read_share_bytes) == FILE_LEN and read_share_bytes[:8] == FILE_MAGIC and read_share_bytes[8] == 1
        except Exception:
            result["embedded_machine_share_valid_header"] = False
        if machine_share_path:
            supplied = read_share(machine_share_path)
            result["embedded_machine_share_matches_supplied"] = supplied == ms.data
    s99 = archive.find("etc/init.d/S99PBA.sh")
    if s99:
        result["s99_contains_old_temp_password_path"] = b"sedutil-password.in" in s99.data
        result["s99_sha256"] = sha256_bytes(s99.data)
    sedtoken = archive.find("sbin/sedtoken")
    if sedtoken:
        result["sedtoken_size"] = len(sedtoken.data)
        result["sedtoken_sha256"] = sha256_bytes(sedtoken.data)
        result["sedtoken_mode_octal"] = oct(sedtoken.mode)
    return result


def write_report(path: Path, info: dict[str, object]) -> None:
    make_parent(path)
    lines = []
    for k in sorted(info.keys()):
        lines.append(f"{k}: {info[k]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_report(info: dict[str, object]) -> None:
    for k in sorted(info.keys()):
        print(f"{k}: {info[k]}")


def cmd_create_token(args: argparse.Namespace) -> int:
    password = validate_password(get_password_from_args(args))
    machine, unlock = create_share_files(password, Path(args.out), force=args.force)
    print(f"Created {machine}")
    print(f"Created {unlock}")
    info = verify_share_pair(machine, unlock)
    print(f"Machine ID: {info['machine_id_hex']}")
    print(f"Password length: {info['password_length']} bytes")
    print("Copy UNLOCK.BIN to the runtime token USB as \\SEDUTIL\\UNLOCK.BIN.")
    return 0


def cmd_verify_shares(args: argparse.Namespace) -> int:
    info = verify_share_pair(Path(args.machine_share), Path(args.unlock_bin))
    print_report(info)
    return 0


def cmd_personalize(args: argparse.Namespace) -> int:
    raw_out, gz_out, report, info = transform_image(
        input_path=Path(args.input),
        machine_share_path=Path(args.machine_share),
        sedtoken_path=Path(args.sedtoken),
        script_path=Path(args.script),
        output=Path(args.output),
        force=args.force,
    )
    print(f"Wrote {raw_out}")
    print(f"Wrote {gz_out}")
    print(f"Wrote {report}")
    print(f"raw_sha256: {info['raw_sha256']}")
    print(f"gz_sha256:  {info['gz_sha256']}")
    print(f"GUIDs preserved: {info['guid_preserved']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    info = inspect_image(Path(args.image), Path(args.machine_share) if args.machine_share else None)
    print_report(info)
    if args.report:
        write_report(Path(args.report), info)
        print(f"Wrote {args.report}")
    # Conservative nonzero exit for obvious missing required pieces in a tokenized PBA.
    required = ["has_linuxpba", "has_sedtoken", "has_machine_share", "has_s99"]
    if not all(bool(info.get(k)) for k in required):
        return 2
    if info.get("s99_contains_old_temp_password_path"):
        return 3
    if args.machine_share and not info.get("embedded_machine_share_matches_supplied"):
        return 4
    return 0


def cmd_install_token_usb(args: argparse.Namespace) -> int:
    src = Path(args.unlock_bin)
    data = read_share(src)
    root = Path(args.usb)
    if not root.exists() or not root.is_dir():
        fail(f"USB root does not exist or is not a directory: {root}")
    dest_dir = root / "SEDUTIL"
    dest = dest_dir / "UNLOCK.BIN"
    if dest.exists() and not args.force:
        fail(f"Refusing to overwrite existing token file: {dest} (use --force)")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    if dest.read_bytes() != data:
        fail("Copied token file did not verify byte-for-byte.")
    print(f"Installed {dest}")
    print(f"sha256: {sha256_bytes(data)}")
    return 0


def cmd_make_all(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    unlock_out: Optional[Path] = None
    if args.usb:
        root = Path(args.usb)
        if not root.exists() or not root.is_dir():
            fail(f"USB root does not exist or is not a directory: {root}")
        unlock_out = root / "SEDUTIL" / "UNLOCK.BIN"
    password = validate_password(get_password_from_args(args))
    machine, unlock = create_share_files(password, out, force=args.force, unlock_out=unlock_out)
    print(f"Created {machine}")
    print(f"Created {unlock}")
    image_output = out / args.name
    raw_out, gz_out, report, info = transform_image(
        input_path=Path(args.input),
        machine_share_path=machine,
        sedtoken_path=Path(args.sedtoken),
        script_path=Path(args.script),
        output=image_output,
        force=args.force,
    )
    print(f"Wrote {raw_out}")
    print(f"Wrote {gz_out}")
    print(f"Wrote {report}")
    print(f"raw_sha256: {info['raw_sha256']}")
    print(f"gz_sha256:  {info['gz_sha256']}")
    if unlock_out is not None:
        print(f"Token installed directly at {unlock}; no UNLOCK.BIN was written to {out}.")
    else:
        print("Next: copy UNLOCK.BIN to the runtime token USB as \\SEDUTIL\\UNLOCK.BIN, or run install-token-usb.")
        print(f"SECURITY: {out} now holds both shares (and the image embeds the machine share); delete it after the token is installed and the PBA is deployed.")
    return 0


def default_script_path() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    p = here / "pba" / "S99PBA.sh"
    return p if p.exists() else None


def add_password_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--password", help="INSECURE: password as command-line text; useful only for disposable tests.")
    p.add_argument("--password-stdin", action="store_true", help="Read one password line from standard input.")
    p.add_argument("--no-confirm", action="store_true", help="Do not ask for password confirmation when prompting.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="sedutil PBA USB-token personalizer")
    parser.add_argument("--version", action="version", version="sedutil_token_pba.py 1.0.0")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-token", help="Generate machine-share.bin and UNLOCK.BIN locally")
    p.add_argument("--out", required=True, help="Output directory for machine-share.bin and UNLOCK.BIN")
    p.add_argument("--force", action="store_true", help="Overwrite existing output files")
    add_password_args(p)
    p.set_defaults(func=cmd_create_token)

    p = sub.add_parser("verify-shares", help="Verify that machine-share.bin and UNLOCK.BIN match without printing the password")
    p.add_argument("--machine-share", required=True)
    p.add_argument("--unlock-bin", required=True)
    p.set_defaults(func=cmd_verify_shares)

    p = sub.add_parser("personalize", help="Inject sedtoken, S99PBA.sh, and machine-share.bin into a PBA image")
    p.add_argument("--input", required=True, help="Original PBA .img or .img.gz")
    p.add_argument("--machine-share", required=True, help="machine-share.bin generated by create-token")
    p.add_argument("--sedtoken", required=True, help="Static Linux sedtoken ELF binary to inject")
    p.add_argument("--script", default=str(default_script_path() or ""), help="S99PBA.sh to inject; defaults to the included pba/S99PBA.sh")
    p.add_argument("--output", required=True, help="Output path/base. Produces both .img and .img.gz")
    p.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    p.set_defaults(func=cmd_personalize)

    p = sub.add_parser("verify", help="Inspect a generated PBA image")
    p.add_argument("--image", required=True, help="PBA .img or .img.gz")
    p.add_argument("--machine-share", help="Optional machine-share.bin to compare with embedded copy")
    p.add_argument("--report", help="Optional path to write verification report")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("install-token-usb", help="Copy UNLOCK.BIN to a USB root as SEDUTIL/UNLOCK.BIN")
    p.add_argument("--unlock-bin", required=True, help="UNLOCK.BIN generated by create-token")
    p.add_argument("--usb", required=True, help="USB root path (your USB stick's drive letter on Windows, e.g. X:\\)")
    p.add_argument("--force", action="store_true", help="Overwrite existing SEDUTIL/UNLOCK.BIN")
    p.set_defaults(func=cmd_install_token_usb)

    p = sub.add_parser("make-all", help="Create shares and personalize an image in one local operation")
    p.add_argument("--input", required=True, help="Original PBA .img or .img.gz")
    p.add_argument("--sedtoken", required=True, help="Static Linux sedtoken ELF binary to inject")
    p.add_argument("--script", default=str(default_script_path() or ""), help="S99PBA.sh to inject; defaults to the included pba/S99PBA.sh")
    p.add_argument("--out", required=True, help="Output directory for shares, images, and report")
    p.add_argument("--name", default="sedutil-token-personalized", help="Output image base name within --out")
    p.add_argument("--usb", help="Write UNLOCK.BIN directly to this USB root (as SEDUTIL\\UNLOCK.BIN) instead of into --out, so the two shares never coexist in the output folder")
    p.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    add_password_args(p)
    p.set_defaults(func=cmd_make_all)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    if sys.version_info < PY_MIN:
        print(f"Python {PY_MIN[0]}.{PY_MIN[1]}+ is required; running {sys.version.split()[0]}", file=sys.stderr)
        return 70
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "script", None) == "":
            fail("No default S99PBA.sh found; pass --script explicitly.")
        return int(args.func(args))
    except ToolError as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
