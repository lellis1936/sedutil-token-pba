"""Tests for sedutil_token_pba.py against fully synthetic PBA images.

No sedutil code, binaries, or images are used. The synthetic image is a
minimal GPT disk with a FAT16 ESP containing /EFI/boot/rootfs.cpio.xz whose
cpio archive holds a fake /sbin/linuxpba, which is everything the
personalizer requires of a base image.
"""

import gzip
import io
import lzma
import os
import stat
import struct
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sedutil_token_pba as stp

SECTOR = 512
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "pba" / "S99PBA.sh"


def lfn_entries(long_name: str) -> bytes:
    """Build on-disk FAT long-filename directory entries (highest sequence first)."""
    units = [ord(c) for c in long_name] + [0x0000]
    while len(units) % 13:
        units.append(0xFFFF)
    count = len(units) // 13
    out = b""
    for seq in range(count, 0, -1):
        chunk = units[(seq - 1) * 13:seq * 13]
        payload = b"".join(struct.pack("<H", u) for u in chunk)
        e = bytearray(32)
        e[0] = seq | (0x40 if seq == count else 0)
        e[11] = 0x0F
        e[1:11] = payload[0:10]
        e[14:26] = payload[10:22]
        e[28:32] = payload[22:26]
        out += bytes(e)
    return out


def dir_entry(short8: bytes, ext3: bytes, attr: int, cluster: int, size: int) -> bytes:
    e = bytearray(32)
    e[0:8] = short8
    e[8:11] = ext3
    e[11] = attr
    e[20:22] = struct.pack("<H", (cluster >> 16) & 0xFFFF)
    e[26:28] = struct.pack("<H", cluster & 0xFFFF)
    e[28:32] = struct.pack("<I", size)
    return bytes(e)


def build_rootfs_xz(include_linuxpba: bool = True) -> bytes:
    entries: list[stp.CpioEntry] = []

    def add(name: str, data: bytes, mode: int) -> None:
        fields = [0] * 13
        fields[0] = len(entries) + 1
        fields[1] = mode
        fields[4] = 1
        entries.append(stp.CpioEntry(name=name, fields=fields, data=data))

    add("sbin", b"", stat.S_IFDIR | 0o755)
    if include_linuxpba:
        add("sbin/linuxpba", b"\x7fELF fake linuxpba for tests\n", stat.S_IFREG | 0o755)
    add("etc", b"", stat.S_IFDIR | 0o755)
    add("etc/init.d", b"", stat.S_IFDIR | 0o755)
    add("TRAILER!!!", b"", 0)
    cpio = stp.CpioNewc(entries).to_bytes()
    return lzma.compress(cpio, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32)


def build_synthetic_pba(rootfs_xz: bytes) -> bytes:
    # FAT16 requires >= 4085 clusters; 4200 one-sector clusters keeps the
    # image around 2 MiB while leaving plenty of free space for growth.
    spc, rsv, nfats, root_entries, spf, data_clusters = 1, 1, 1, 64, 17, 4200
    root_secs = (root_entries * 32 + SECTOR - 1) // SECTOR
    totsec = rsv + nfats * spf + root_secs + data_clusters
    part_first_lba = 34
    part_last_lba = part_first_lba + totsec - 1
    img = bytearray((part_last_lba + 1) * SECTOR)

    img[510:512] = b"\x55\xaa"  # protective MBR signature

    hdr = bytearray(92)
    hdr[0:8] = b"EFI PART"
    hdr[56:72] = uuid.uuid4().bytes_le
    hdr[72:80] = struct.pack("<Q", 2)   # partition array LBA
    hdr[80:84] = struct.pack("<I", 8)   # entry count
    hdr[84:88] = struct.pack("<I", 128)  # entry size
    img[SECTOR:SECTOR + 92] = hdr

    e = bytearray(128)
    e[0:16] = stp.EFI_PARTITION_TYPE.bytes_le
    e[16:32] = uuid.uuid4().bytes_le
    e[32:40] = struct.pack("<Q", part_first_lba)
    e[40:48] = struct.pack("<Q", part_last_lba)
    e[56:128] = "EFI System".encode("utf-16le").ljust(72, b"\x00")
    img[2 * SECTOR:2 * SECTOR + 128] = e

    part_off = part_first_lba * SECTOR
    bs = bytearray(SECTOR)
    bs[11:13] = struct.pack("<H", SECTOR)
    bs[13] = spc
    bs[14:16] = struct.pack("<H", rsv)
    bs[16] = nfats
    bs[17:19] = struct.pack("<H", root_entries)
    bs[19:21] = struct.pack("<H", totsec)
    bs[22:24] = struct.pack("<H", spf)
    bs[510:512] = b"\x55\xaa"
    img[part_off:part_off + SECTOR] = bs

    fat_off = part_off + rsv * SECTOR

    def set_fat(c: int, v: int) -> None:
        img[fat_off + 2 * c:fat_off + 2 * c + 2] = struct.pack("<H", v)

    set_fat(0, 0xFFF8)
    set_fat(1, 0xFFFF)
    set_fat(2, 0xFFFF)  # /EFI directory
    set_fat(3, 0xFFFF)  # /EFI/boot directory

    root_off = part_off + (rsv + nfats * spf) * SECTOR
    data_off = root_off + root_secs * SECTOR

    def clus_off(c: int) -> int:
        return data_off + (c - 2) * spc * SECTOR

    img[root_off:root_off + 32] = dir_entry(b"EFI     ", b"   ", 0x10, 2, 0)
    img[clus_off(2):clus_off(2) + 32] = dir_entry(b"BOOT    ", b"   ", 0x10, 3, 0)

    file_clusters = max(1, -(-len(rootfs_xz) // (spc * SECTOR)))
    chain = list(range(4, 4 + file_clusters))
    for i, c in enumerate(chain):
        set_fat(c, chain[i + 1] if i + 1 < len(chain) else 0xFFFF)
    boot_dir = lfn_entries("rootfs.cpio.xz") + dir_entry(b"ROOTFS~1", b"XZ ", 0x20, 4, len(rootfs_xz))
    img[clus_off(3):clus_off(3) + len(boot_dir)] = boot_dir
    padded = rootfs_xz + b"\x00" * (file_clusters * spc * SECTOR - len(rootfs_xz))
    img[clus_off(4):clus_off(4) + len(padded)] = padded
    return bytes(img)


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_roundtrip(self):
        password = stp.validate_password("Correct Horse 42!")
        machine, unlock = stp.create_share_files(password, self.out)
        info = stp.verify_share_pair(machine, unlock)
        self.assertEqual(info["password_length"], len(password))

    def test_share_alone_is_not_password(self):
        password = stp.validate_password("hunter2hunter2")
        machine, unlock = stp.create_share_files(password, self.out)
        for share in (machine, unlock):
            self.assertNotIn(b"hunter2", share.read_bytes())

    def test_tampered_share_fails(self):
        password = stp.validate_password("pw123456")
        machine, unlock = stp.create_share_files(password, self.out)
        data = bytearray(unlock.read_bytes())
        data[stp.HEADER_LEN] ^= 0xFF  # corrupt record magic
        unlock.write_bytes(bytes(data))
        with self.assertRaises(stp.ToolError):
            stp.verify_share_pair(machine, unlock)

    def test_mismatched_machine_ids_fail(self):
        password = stp.validate_password("pw123456")
        machine, unlock = stp.create_share_files(password, self.out)
        other = self.out / "other"
        stp.create_share_files(password, other)
        with self.assertRaises(stp.ToolError):
            stp.verify_share_pair(machine, other / "UNLOCK.BIN")

    def test_unlock_out_keeps_shares_apart(self):
        password = stp.validate_password("RedirectTest99")
        build = self.out / "build"
        usb = self.out / "usbroot"
        usb.mkdir()
        machine, unlock = stp.create_share_files(
            password, build, unlock_out=usb / "SEDUTIL" / "UNLOCK.BIN")
        self.assertEqual(unlock, usb / "SEDUTIL" / "UNLOCK.BIN")
        self.assertFalse((build / "UNLOCK.BIN").exists())
        info = stp.verify_share_pair(machine, unlock)
        self.assertEqual(info["password_length"], len(password))

    def test_password_validation(self):
        with self.assertRaises(stp.ToolError):
            stp.validate_password("pässword")  # non-ASCII
        with self.assertRaises(stp.ToolError):
            stp.validate_password("")
        with self.assertRaises(stp.ToolError):
            stp.validate_password("x" * (stp.MAX_PASSWORD + 1))
        with self.assertRaises(stp.ToolError):
            stp.validate_password("tab\there")


class CpioTests(unittest.TestCase):
    def test_read_write_roundtrip(self):
        xz = build_rootfs_xz()
        cpio = lzma.decompress(xz)
        archive = stp.CpioNewc.read(cpio)
        self.assertTrue(archive.has("sbin/linuxpba"))
        reread = stp.CpioNewc.read(archive.to_bytes())
        self.assertEqual([e.name for e in reread.entries], [e.name for e in archive.entries])

    def test_upsert_and_ensure_dir(self):
        archive = stp.CpioNewc.read(lzma.decompress(build_rootfs_xz()))
        archive.ensure_dir("etc/sedutil")
        archive.upsert_file("sbin/sedtoken", b"payload", stat.S_IFREG | 0o755)
        archive.upsert_file("sbin/sedtoken", b"payload2", stat.S_IFREG | 0o755)  # overwrite
        reread = stp.CpioNewc.read(archive.to_bytes())
        entry = reread.find("sbin/sedtoken")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.data, b"payload2")
        self.assertEqual(reread.entries[-1].name, "TRAILER!!!")


class TransformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        password = stp.validate_password("SyntheticTestPw1")
        self.machine, self.unlock = stp.create_share_files(password, self.dir)
        # Incompressible payload larger than the original rootfs forces the
        # FAT replacer down its cluster-growth path.
        self.sedtoken = self.dir / "sedtoken.bin"
        self.sedtoken.write_bytes(b"\x7fELF" + os.urandom(150_000))
        self.input_img = self.dir / "base.img"
        self.input_img.write_bytes(build_synthetic_pba(build_rootfs_xz()))

    def transform(self, input_path: Path):
        return stp.transform_image(
            input_path=input_path,
            machine_share_path=self.machine,
            sedtoken_path=self.sedtoken,
            script_path=SCRIPT_PATH,
            output=self.dir / "personalized",
            force=True,
        )

    def test_transform_and_verify(self):
        raw_out, gz_out, report_path, report = self.transform(self.input_img)
        self.assertTrue(report["guid_preserved"])
        self.assertEqual(report["input_size"], report["output_size"])
        self.assertTrue(report_path.exists())

        info = stp.inspect_image(raw_out, self.machine)
        for key in ("has_linuxpba", "has_sedtoken", "has_machine_share", "has_s99"):
            self.assertTrue(info[key], key)
        self.assertTrue(info["embedded_machine_share_matches_supplied"])
        self.assertFalse(info["s99_contains_old_temp_password_path"])
        self.assertEqual(info["sedtoken_sha256"], stp.sha256_file(self.sedtoken))

        # gz output decompresses to the raw output
        self.assertEqual(gzip.decompress(gz_out.read_bytes()), raw_out.read_bytes())

    def test_gzip_input(self):
        gz_in = self.dir / "base.img.gz"
        gz_in.write_bytes(gzip.compress(self.input_img.read_bytes()))
        raw_out, _, _, report = self.transform(gz_in)
        self.assertTrue(report["input_was_gzip"])
        self.assertTrue(stp.inspect_image(raw_out)["has_sedtoken"])

    def test_guids_preserved_exactly(self):
        before = stp.parse_gpt(self.input_img.read_bytes())
        raw_out, _, _, _ = self.transform(self.input_img)
        after = stp.parse_gpt(raw_out.read_bytes())
        self.assertEqual(before.disk_guid, after.disk_guid)
        self.assertEqual(before.first_part_unique_guid, after.first_part_unique_guid)

    def test_make_all_usb_end_to_end(self):
        usb = self.dir / "usb"
        usb.mkdir()
        outdir = self.dir / "ma-out"
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("EndToEnd42!\n")
        try:
            rc = stp.main([
                "make-all",
                "--input", str(self.input_img),
                "--sedtoken", str(self.sedtoken),
                "--script", str(SCRIPT_PATH),
                "--out", str(outdir),
                "--usb", str(usb),
                "--password-stdin",
            ])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        self.assertFalse((outdir / "UNLOCK.BIN").exists())
        token = usb / "SEDUTIL" / "UNLOCK.BIN"
        self.assertTrue(token.exists())
        info = stp.inspect_image(outdir / "sedutil-token-personalized.img", outdir / "machine-share.bin")
        self.assertTrue(info["embedded_machine_share_matches_supplied"])
        stp.verify_share_pair(outdir / "machine-share.bin", token)

    def test_missing_linuxpba_refused(self):
        bad = self.dir / "bad.img"
        bad.write_bytes(build_synthetic_pba(build_rootfs_xz(include_linuxpba=False)))
        with self.assertRaises(stp.ToolError):
            self.transform(bad)

    def test_refuses_overwrite_without_force(self):
        self.transform(self.input_img)
        with self.assertRaises(stp.ToolError):
            stp.transform_image(
                input_path=self.input_img,
                machine_share_path=self.machine,
                sedtoken_path=self.sedtoken,
                script_path=SCRIPT_PATH,
                output=self.dir / "personalized",
                force=False,
            )


class Fat16GuardTests(unittest.TestCase):
    def test_fat12_sized_volume_rejected(self):
        img = bytearray(build_synthetic_pba(build_rootfs_xz()))
        part_off = 34 * SECTOR
        # Shrink the sector count so the cluster count drops below the FAT16
        # minimum; the parser must refuse rather than misread FAT12 chains.
        img[part_off + 19:part_off + 21] = struct.pack("<H", 1 + 17 + 4 + 1000)
        with self.assertRaises(stp.ToolError):
            stp.Fat16Image(img, part_off)


if __name__ == "__main__":
    unittest.main()
