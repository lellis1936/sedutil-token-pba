# sedutil-token-pba

USB-token unlock for sedutil (TCG Opal) pre-boot authentication.

This is a companion tool for [sedutil](https://github.com/Drive-Trust-Alliance/sedutil), the Drive Trust Alliance's self-encrypting-drive utility. It takes an existing sedutil UEFI PBA image and produces a personalized copy that unlocks the drive automatically when a USB token is plugged in at boot, falling back to the normal keyboard prompt when the token is absent. It is developed and tested against PBA images from the maintained [ChubbyAnt sedutil fork](https://github.com/ChubbyAnt/sedutil).

The objective is to facilitate headless operation so that the machine can be booted without manual password entry.

**You should be an experienced sedutil user if you intend to use this program to patch your PBA image.  You should have a tested, bootable sedutil flash drive as well as a tested, bootable sedutil recovery flash drive.  THIS IS NOT OPTIONAL!**

## No sedutil code or binaries in this repository

This repository intentionally contains **no sedutil source code, binaries, or PBA disk images**, and neither its releases nor its CI ever distribute them. You bring your own base PBA image (see below); this tool only rewrites your local copy of it. Everything in this repository — the Python personalizer and the small C `sedtoken` helper — is original, MIT-licensed code.

## How it works

The drive password is split into two shares (2-of-2 XOR — see [SECURITY.md](SECURITY.md)):

- `machine-share.bin` — embedded in the personalized PBA image at `/etc/sedutil/machine-share.bin`
- `UNLOCK.BIN` — copied to a USB stick as `\SEDUTIL\UNLOCK.BIN` (the token)

Neither file alone reveals the password. At boot, the PBA's `S99PBA.sh` scans removable media for the token; if found, `sedtoken` reconstructs the password in memory and pipes it directly to `linuxpba` stdin — no temporary files. If no token turns up after a few scans, it falls back to standard keyboard entry, so a lost token never locks you out of the keyboard path.

## Base image (bring your own)

The personalizer needs a compatible sedutil UEFI PBA image as input. The tested base image is `UEFI64--1.15-5ad84d8.img.gz` from the ChubbyAnt sedutil releases:

| Base image | SHA-256 of decompressed `.img` |
|---|---|
| `UEFI64--1.15-5ad84d8.img.gz` | `bdcd0399a01b063c7132b79de3c3ecf7ec476fd4d40af677db3f221dcda36462` |

Other sedutil UEFI PBA images of the same shape (GPT with a FAT16 EFI System Partition containing `\EFI\BOOT\rootfs.cpio.xz` and `/sbin/linuxpba`) may work but are untested; the tool fails closed if the image doesn't match what it expects.

**IMPORTANT.  You should use the same 64-bit UEFI PBA image that you currently have loaded to the boot drive.  This will avoid any risk of having to update UEFI NVRAM entries once you have loaded the new PBA image to the drive.**


## Host OS model

- **Windows**: recommended for share generation and PBA personalization.
- **GitHub Actions or Linux**: builds the Linux `/sbin/sedtoken` ELF binary from `src/sedtoken.c`.
- **sedutil rescue Linux / external boot**: recommended for `sedutil-cli --loadpbaimage` and Shadow MBR maintenance. Do not do Shadow MBR operations from live Windows booted from the same protected drive.

## Requirements

Python **3.12+**. No third-party packages — standard library only. The script runs on Windows or Linux, but has only been tested under Windows.

The multi-line command examples below use PowerShell backtick (`` ` ``) line continuation, so run them in PowerShell (5.1+, built into Windows 10/11) — not cmd.exe or bash. To use another shell, join each command onto a single line.

## Included commands

```text
create-token        Generate machine-share.bin and UNLOCK.BIN locally
verify-shares       Verify the two share files match without printing the password
personalize         Inject sedtoken, S99PBA.sh, and machine-share.bin into a PBA image
verify              Inspect/verify a generated PBA image
install-token-usb   Copy UNLOCK.BIN to USB:\SEDUTIL\UNLOCK.BIN
make-all            Create shares and personalize a PBA in one command
```

## Typical workflow

### 1. Get sedtoken

The PBA runs Linux, so `sedtoken` must be a Linux ELF binary. Download `sedtoken-linux-x86_64` and its `.sha256` from this repository's [Releases](../../releases) page — it is built from `src/sedtoken.c` by CI on every release tag. Verify the hash, then place it at:

```text
bin/sedtoken-linux-x86_64
```

Alternatively, build it yourself on any Linux machine:

```sh
gcc -static -Os -s -Wall -Wextra -o sedtoken-linux-x86_64 src/sedtoken.c
sha256sum sedtoken-linux-x86_64
```

### 2. All-in-one local build

This creates both shares and a personalized PBA image. It accepts either `.img` or `.img.gz` input and produces both `.img` and `.img.gz` output. It prompts for the drive password.

```powershell
python sedutil_token_pba.py make-all `
  --input UEFI64--1.15-5ad84d8.img.gz `
  --sedtoken .\bin\sedtoken-linux-x86_64 `
  --out C:\SedutilTokenBuild
```

(`--script` defaults to the included `pba\S99PBA.sh`.)

Optionally add `--usb X:\` (your token stick's drive letter) to write `UNLOCK.BIN` directly to the stick as `X:\SEDUTIL\UNLOCK.BIN` instead of into `--out`. This is recommended: the two shares then never coexist in the output folder, and step 3 below is already done.

Outputs:

```text
C:\SedutilTokenBuild\machine-share.bin
C:\SedutilTokenBuild\UNLOCK.BIN
C:\SedutilTokenBuild\sedutil-token-personalized.img
C:\SedutilTokenBuild\sedutil-token-personalized.img.gz
C:\SedutilTokenBuild\sedutil-token-personalized.img.verify.txt
```

**The files in this folder are sensitive.  Together (or even UNLOCK.BIN plus the image, which embeds the machine share) they reconstruct your sedutil password.  Keep the folder out of backups and cloud sync, and delete it after deployment — see step 6.**  


### 3. Copy the USB token file

Skip this step if you used `--usb` in step 2.

Manual copy is fine:

```text
C:\SedutilTokenBuild\UNLOCK.BIN -> X:\SEDUTIL\UNLOCK.BIN
```

Or use:

```powershell
python sedutil_token_pba.py install-token-usb `
  --unlock-bin C:\SedutilTokenBuild\UNLOCK.BIN `
  --usb X:\
```

**Replace `X:\` with your USB stick's drive letter.**

### 4. Verify the image

```powershell
python sedutil_token_pba.py verify `
  --image C:\SedutilTokenBuild\sedutil-token-personalized.img.gz `
  --machine-share C:\SedutilTokenBuild\machine-share.bin
```

### 5. Test and install

For a USB boot test, write the `.img.gz` to a spare USB stick with Balena Etcher or Rufus and boot it with the token stick also inserted.

To install into the drive's Shadow MBR, use the raw `.img` with `sedutil-cli` from rescue media (not from Windows booted off the protected drive):

**BEFORE DOING SO, VERIFY THAT YOUR UEFI image source file is the one your boot drive is using. IF YOU CAN'T DO THAT, YOU SHOULD NOT CONTINUE SINCE YOU MIGHT LOSE THE ABILITY TO BOOT YOUR SYSTEM.**

```text
sedutil-cli --loadPBAimage <password> sedutil-token-personalized.img \\.\PhysicalDrive0
```
**THIS ASSUMES YOUR OPAL BOOT DRIVE IS PHYSICALDRIVE0**


Passing the password on the command line leaves it in your shell history and process list — clear the history afterward, and adjust the drive path (`\\.\PhysicalDrive0` on Windows, `/dev/sda`-style on Linux) to the protected drive.

### 6. Clean up

Once the token is installed and the deployed PBA is boot-tested, **delete the entire output folder** (e.g. `C:\SedutilTokenBuild`). It is scaffolding, not storage: everything in it either holds one of the shares or embeds one, and the folder as a whole is password-equivalent. The canonical homes of the shares are the drive's Shadow MBR (machine share, inside the loaded PBA) and the token stick (`UNLOCK.BIN`) — physically separate, as designed. If you ever need to rebuild, rerun `make-all` with the password.

## What the personalizer changes

Inside the PBA rootfs it injects/replaces:

```text
/sbin/sedtoken
/etc/init.d/S99PBA.sh
/etc/sedutil/machine-share.bin
```

It preserves the original GPT disk GUID and EFI partition GUID because it modifies a copy of the existing image and replaces only `\EFI\BOOT\rootfs.cpio.xz` inside the existing FAT ESP. Preserved GUIDs matter when a UEFI NVRAM boot entry points at the PBA partition.

## Implementation notes

This tool does not create a brand-new FAT image from scratch. It uses the existing PBA image as a template and does a narrow, fail-closed replacement of `\EFI\BOOT\rootfs.cpio.xz` inside the FAT16 EFI partition. That is intentional because it preserves the working layout and GUIDs. See [docs/IMPLEMENTATION-NOTES.md](docs/IMPLEMENTATION-NOTES.md) for details.

Limitations:

- Supports compatible ChubbyAnt/sedutil UEFI PBA images only.
- Expects a GPT image with the first partition as a FAT16 EFI System Partition (FAT12-sized volumes are rejected).
- Expects `\EFI\BOOT\rootfs.cpio.xz` and `/sbin/linuxpba`.
- v1 share format supports printable ASCII passwords from 1 to 256 bytes.

## Security model

- `machine-share.bin` alone does not reveal the password.
- `UNLOCK.BIN` alone does not reveal the password.
- Both together reconstruct the password record — treat the USB token like a physical key.
- `sedtoken` pipes the reconstructed password directly to `linuxpba` and wipes its buffers; no password ever touches a file.
- The Python share-generation step necessarily has the password in Python process memory while creating the two shares.

See [SECURITY.md](SECURITY.md) for the full model and known limitations.

## License

[MIT](LICENSE).

## Acknowledgments

- The [Drive Trust Alliance](https://github.com/Drive-Trust-Alliance/sedutil) for sedutil and the original PBA.
- [ChubbyAnt](https://github.com/ChubbyAnt/sedutil) for the maintained sedutil fork whose PBA images this tool targets.
