# Security model

## Design

The drive password is split into two 512-byte shares with a 2-of-2 XOR scheme:

- `machine-share.bin` — embedded inside the personalized PBA image
  (`/etc/sedutil/machine-share.bin`), which lives in the drive's Opal shadow
  MBR.
- `UNLOCK.BIN` — stored on a USB stick at `\SEDUTIL\UNLOCK.BIN`. This is the
  physical key.

One share XORed with random data is itself indistinguishable from random:
neither file alone reveals anything about the password. Both shares carry a
matching random machine ID so mismatched pairs are rejected.

At boot, `sedtoken` reconstructs the password in an `mlock`ed buffer, pipes
it directly to `linuxpba` stdin (no temporary files), zeroes the buffer, and
disables core dumps (`RLIMIT_CORE = 0`).

## What this protects against — and what it doesn't

- An attacker with the **machine/drive but not the token** cannot recover the
  password from the shadow MBR: the machine share alone is uniform random
  data. They face the same locked drive as without this tool.
- An attacker with the **token but not the drive** likewise learns nothing:
  `UNLOCK.BIN` alone is uniform random data.
- An attacker with **both the drive and the token** can reconstruct the
  password. The USB token must be treated exactly like a physical key.
- The machine share being readable by whoever holds the drive is **by
  design** — that is what the 2-of-2 split is for.

## Known limitations (v1, by design)

- Shares are **unauthenticated**: integrity is checked only by structure
  (magic values, matching machine IDs, printable-ASCII password). There is no
  MAC. Note that PBA-image integrity is out of scope anyway — the Opal shadow
  MBR is writable before authentication by anyone with the drive.
- Passwords are limited to printable US-ASCII, 1–256 bytes.
- During share **creation**, the password necessarily exists in Python
  process memory on the (trusted) personalization machine.
- `sedtoken` runs as the only user (root) in the PBA's single-user initramfs,
  so some hardening steps are intentionally omitted as unnecessary in that
  environment (e.g. the `/tmp/pbaerror.log` stderr redirect is opened without
  `O_NOFOLLOW`, and `PR_SET_DUMPABLE` is not cleared).

## Operational guidance

- **The build folder is password-equivalent.** `make-all` output contains both
  shares — and the personalized image embeds the machine share, so even
  `UNLOCK.BIN` plus the image reconstructs the password. Delete the folder
  after deployment, and never let it reach backups, cloud sync, or another
  machine. Prefer `make-all --usb X:\`, which writes `UNLOCK.BIN` directly to
  the token stick so the pair never coexists on disk at all (the only
  remaining coexistence is process memory during creation).
- **Store the token away from the machine.** The token is only needed during
  the boot scan; remove it afterward. The design's security boundary is the
  physical separation of drive and token — a machine stolen with its token
  is unlockable by the thief, exactly like a door with the key left in it.
- **Use a dedicated random password.** If the sedutil password is unique to
  the drive and random (e.g. several diceware words — it must still be
  typeable for keyboard fallback), then an attacker who captures both shares
  learns nothing reusable: unlocking the drive is all it grants, which mere
  possession of both artifacts already implied.
- **Rotation is cheap.** If either share may be compromised: change the drive
  password with `sedutil-cli`, rerun `make-all`, reload the PBA, rewrite the
  token. Both old shares immediately become useless.

## Reporting

Please report suspected vulnerabilities via a GitHub issue on this
repository. There is no bug bounty; this is a personal open-source project.
