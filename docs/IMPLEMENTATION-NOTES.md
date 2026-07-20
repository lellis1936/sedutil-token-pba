# Implementation notes

The Python tool is intentionally narrow and conservative.

## Image handling

Input may be raw `.img` or gzip-compressed `.img.gz`. The program detects gzip by magic bytes, not filename alone.

The output is always both raw `.img` and `.img.gz`.

## GPT/GUID behavior

The tool parses the primary GPT header and first partition entry, then modifies a copy of the original image. It does not rewrite GPT headers or partition entries. Therefore the disk GUID and EFI partition unique GUID should remain stable.

This matters for UEFI NVRAM boot entries that point to a specific PBA partition GUID.

## FAT behavior

The implementation is a limited FAT16 file replacer for the expected sedutil PBA ESP. It is not a general FAT editor. It fails closed rather than guessing on anything outside the shape it expects:

- Only FAT16 by cluster count (4085..65524) is accepted; FAT12 (packed 1.5-byte entries this reader does not implement) and FAT32 (different BPB/root-directory layout) are rejected.
- Cluster chains are validated on traversal: bad-cluster (`0xFFF7`), reserved values, out-of-range cluster numbers, and cycles are errors, not silent truncation.
- The declared volume must fit within the image, and the FAT must be large enough to map every cluster, so a malformed BPB cannot steer reads or writes out of bounds. (The volume may legitimately overhang the GPT partition by a few unused sectors — real ChubbyAnt images do — so that is not treated as an error.)
- The first GPT partition must be an EFI System Partition (type GUID `c12a7328-...`); intermediate path components must actually be directories.

It can read directories with long filename entries and replace `\EFI\BOOT\rootfs.cpio.xz`, extending the file into free clusters if needed. When updating the directory entry's size it follows the directory's cluster chain rather than assuming the clusters are physically contiguous, so it stays correct for a fragmented subdirectory.

The `S99PBA.sh` boot script is *replaced*, not augmented, so the input must already contain `/etc/init.d/S99PBA.sh` (every bootable buildroot PBA has, identically, since 2017; rescue images strip it and are not valid targets). The original script's hash and size are recorded in the verify report, and a warning is printed if it does not reference `linuxpba` (a sign of a customized boot script being overwritten).

## Rootfs behavior

The tool uses Python `lzma` to decompress/compress `rootfs.cpio.xz` and a built-in cpio `newc` reader/writer.

It injects:

```text
/sbin/sedtoken                         mode 0755
/etc/init.d/S99PBA.sh                  mode 0755
/etc/sedutil/machine-share.bin         mode 0600
```

## Testing

An automated suite in `tests/` runs in CI (Linux and Windows) against fully synthetic images — a minimal GPT disk with a FAT16 ESP built from scratch in the test code — covering share roundtrip/tamper cases, cpio read/write, the full transform (including FAT cluster growth), GUID preservation, the fail-closed guards (FAT12/FAT32 rejection, corrupt-chain rejection, out-of-image volume, non-ESP partition, missing boot script, fragmented-directory offset mapping), and the direct-to-USB token flow. No sedutil content is involved.

CI can never exercise a real sedutil image (this repository distributes no sedutil content), so validation against real PBA images happens outside CI in two ways:

- Every transformation writes a `.verify.txt` report, and the `verify` command re-checks any generated image: GUIDs preserved, embedded `machine-share.bin` matches the supplied share, injected files present, no legacy temp-password path in the boot script. It exits nonzero if anything required is missing.
- Personalized images built from the real base image are boot-tested on real hardware: the USB token unlocks the drive, and keyboard fallback works when no token is present.
