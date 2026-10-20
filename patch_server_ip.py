#!/usr/bin/env python3
"""Patch the BB10 io-asr binary with a new assistant server IP/port.

The patched io-asr carries the Dragon/Nuance replacement server URL as a
fixed-size string at two offsets (see KNOWLEDGE.md, section "Пач рецепта").
This tool rewrites both occurrences.

Usage:
    python patch_server_ip.py io-asr-patched.bin 192.168.0.157 8080
    python patch_server_ip.py io-asr-patched.bin 192.168.0.157 8080 -o out.bin
"""
import sys

OFFSET_MAIN = 0x2A42D4   # 131-byte field
OFFSET_SECOND = 0x3631CC # 32-byte field
FIELD_MAIN = 131
FIELD_SECOND = 32


def build_field(url: str, size: int) -> bytes:
    # "XX" prefix compensates a 2-byte trim done by io-asr (see KNOWLEDGE.md)
    raw = ("XX" + url).encode()
    if len(raw) > size - 1:
        raise SystemExit(f"URL too long for field ({len(raw)} > {size - 1})")
    return raw + b"\x00" * (size - len(raw))


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        raise SystemExit(1)
    src, ip, port = sys.argv[1], sys.argv[2], sys.argv[3]
    out = sys.argv[5] if len(sys.argv) > 5 and sys.argv[4] == "-o" else src
    url = f"http://{ip}:{port}"

    data = bytearray(open(src, "rb").read())
    for off, size in ((OFFSET_MAIN, FIELD_MAIN), (OFFSET_SECOND, FIELD_SECOND)):
        old = bytes(data[off:off + size]).split(b"\x00")[0]
        print(f"0x{off:x}: {old!r} -> {'XX' + url!r}")
        data[off:off + size] = build_field(url, size)
    open(out, "wb").write(bytes(data))
    print(f"written: {out}")


if __name__ == "__main__":
    main()
