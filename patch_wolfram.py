#!/usr/bin/env python3
"""Patch libWolframPlugin.so (10.3.3.3216, 116272 bytes, cksum 2029093847)
to point at our assistant server instead of dead wolframalpha.com.
Offsets from docs/08-wolfram.md of the bb10-assistant-protocol repo."""
import sys

SRC = r"E:\bb10-assistant-server\libWolframPlugin.so.orig"
DST = r"E:\bb10-assistant-server\libWolframPlugin.so.patched"

SERVER = "http://192.168.0.157:8080"


def patch_string(data, off, expected, new, space):
    old = bytes(data[off:off + len(expected)])
    assert old == expected, (f"offset {off:#x}: expected {expected!r}, "
                             f"found {old!r}")
    assert len(new) < space, f"new string too long: {len(new)} >= {space}"
    data[off:off + space] = new + b"\0" * (space - len(new))


def patch_nop(data, off, expected):
    old = bytes(data[off:off + 2])
    assert old == expected, f"offset {off:#x}: expected {expected!r}, found {old!r}"
    data[off:off + 2] = b"\x00\xbf"  # Thumb-2 NOP


def main():
    data = bytearray(open(SRC, "rb").read())
    assert len(data) == 116272, len(data)

    rec_url = (SERVER + "/wa/recognizer").encode()
    q_url = (SERVER + "/wa/q").encode()

    # recognizer URL (54 bytes space)
    patch_string(data, 0xe0a1,
                 b"http://www.wolframalpha.com/queryrecognizer/query.jsp",
                 rec_url, 54)
    # answer query URL (37 bytes space)
    patch_string(data, 0xde1d,
                 b"http://api.wolframalpha.com/v2/query",
                 q_url, 37)
    # recognizer accept checks -> NOP (accept every question)
    patch_nop(data, 0xb170, b"\x55\xd0")   # beq.n
    patch_nop(data, 0xb1ec, b"\x7d\xb1")   # cbz
    # tap-on-card -> google search instead of m.wolframalpha.com (32 bytes)
    patch_string(data, 0xd8ab,
                 b"http://m.wolframalpha.com/input/",
                 b"https://www.google.com/search", 32)

    open(DST, "wb").write(data)
    print("patched ->", DST)
    print("recognizer:", rec_url.decode())
    print("query     :", q_url.decode())


if __name__ == "__main__":
    main()
