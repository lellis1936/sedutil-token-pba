# Implementation notes

The Python tool is intentionally narrow and conservative.

## Image handling

Input may be raw `.img` or gzip-compressed `.img.gz`. The program detects gzip by magic bytes, not filename alone.

The output is always both raw `.img` and `.img.gz`.

## GPT/GUID behavior

The tool parses the primary GPT header and first partition entry, then modifies a copy of the original image. It does not rewrite GPT headers or partition entries. Therefore the disk GUID and EFI partition unique GUID should remain stable.

This matters for UEFI NVRAM boot entries that point to a specific PBA partition GUID.

## FAT behavior

The implementation is a limited FAT16 file replacer for the expected sedutil PBA ESP. It is not a general FAT editor. Volumes with fewer than 4085 clusters (FAT12 by definition, with packed 1.5-byte FAT entries this reader does not implement) are rejected rather than misread.

It can read directories with long filename entries and replace `\EFI\BOOT\rootfs.cpio.xz`, extending the file into free clusters if needed.

## Rootfs behavior

The tool uses Python `lzma` to decompress/compress `rootfs.cpio.xz` and a built-in cpio `newc` reader/writer.

It injects:

```text
/sbin/sedtoken                         mode 0755
/etc/init.d/S99PBA.sh                  mode 0755
/etc/sedutil/machine-share.bin         mode 0600
```

## Testing

An automated suite in `tests/` runs in CI (Linux and Windows) against fully synthetic images — a minimal GPT disk with a FAT16 ESP built from scratch in the test code — covering share roundtrip/tamper cases, cpio read/write, the full transform (including FAT cluster growth), GUID preservation, and the FAT12 rejection guard. No sedutil content is involved.

CI can never exercise a real sedutil image (this repository distributes no sedutil content), so validation against real PBA images happens outside CI in two ways:

- Every transformation writes a `.verify.txt` report, and the `verify` command re-checks any generated image: GUIDs preserved, embedded `machine-share.bin` matches the supplied share, injected files present, no legacy temp-password path in the boot script. It exits nonzero if anything required is missing.
- Personalized images built from the real base image are boot-tested on real hardware: the USB token unlocks the drive, and keyboard fallback works when no token is present.
