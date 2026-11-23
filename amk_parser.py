"""Parser for Robot-Soft Automatic Mouse and Keyboard (.amk) text scripts."""

from __future__ import annotations

import base64
import os
import struct
from typing import Any

import cv2
import numpy as np


class AmkParseError(ValueError):
    """Raised when an .amk file cannot be parsed or contains malformed data."""


def parse_amk_text(text: str) -> dict[str, Any]:
    """Parse decoded AMK text (@AS, VER, I) into nested Python dicts/lists."""
    n = len(text)
    i = 0

    def skip_ws() -> None:
        nonlocal i
        while i < n and text[i] in " \t\r\n":
            i += 1

    def parse_str() -> str:
        nonlocal i
        if i >= n or text[i] != '"':
            raise AmkParseError(f"Expected '\"' at offset {i}")
        i += 1
        out: list[str] = []
        while i < n:
            c = text[i]
            if c == "\\":
                i += 1
                if i >= n:
                    raise AmkParseError("Unterminated escape in string")
                esc = text[i]
                if esc == "n":
                    out.append("\n")
                elif esc == "r":
                    out.append("\r")
                elif esc == "t":
                    out.append("\t")
                elif esc == '"':
                    out.append('"')
                elif esc == "\\":
                    out.append("\\")
                elif esc == "u" and i + 4 < n:
                    hex_part = text[i + 1 : i + 5]
                    try:
                        out.append(chr(int(hex_part, 16)))
                        i += 4
                    except ValueError:
                        out.append("\\u")
                else:
                    out.append("\\" + esc)
                i += 1
            elif c == '"':
                i += 1
                return "".join(out)
            else:
                out.append(c)
                i += 1
        raise AmkParseError("Unterminated string literal")

    def parse_atom() -> int | float | str:
        nonlocal i
        start = i
        while i < n and text[i] not in ",}]:\r\n \t":
            i += 1
        if i == start:
            raise AmkParseError(f"Unexpected token {text[i:i+20]!r} at offset {i}")
        s = text[start:i]
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return s

    def parse_arr() -> list[Any]:
        nonlocal i
        if i >= n or text[i] != "[":
            raise AmkParseError(f"Expected '[' at offset {i}")
        i += 1
        res: list[Any] = []
        while True:
            skip_ws()
            if i >= n:
                raise AmkParseError("Unterminated array")
            if text[i] == "]":
                i += 1
                return res
            res.append(parse_val())
            skip_ws()
            if i < n and text[i] == ",":
                i += 1

    def parse_obj() -> dict[str, Any]:
        nonlocal i
        if i >= n or text[i] != "{":
            raise AmkParseError(f"Expected '{{' at offset {i}")
        i += 1
        res: dict[str, Any] = {}
        while True:
            skip_ws()
            if i >= n:
                raise AmkParseError("Unterminated object")
            if text[i] == "}":
                i += 1
                return res
            if text[i] == '"':
                key = parse_str()
            else:
                start = i
                while i < n and text[i] not in ":\r\n \t,}]":
                    i += 1
                key = text[start:i]
                if not key:
                    raise AmkParseError(f"Empty key at offset {i}: {text[i:i+20]!r}")
            skip_ws()
            if i >= n or text[i] != ":":
                raise AmkParseError(f"Expected ':' after key {key!r} at offset {i}")
            i += 1
            val = parse_val()
            res[key] = val
            skip_ws()
            # AMK omits commas after nested objects (e.g., #XO:{...}\r\n#YO:{...})
            if i < n and text[i] == ",":
                i += 1

    def parse_val() -> Any:
        skip_ws()
        if i >= n:
            raise AmkParseError("Unexpected end of AMK input")
        c = text[i]
        if c == "{":
            return parse_obj()
        if c == "[":
            return parse_arr()
        if c == '"':
            return parse_str()
        return parse_atom()

    root = parse_val()
    skip_ws()
    if i != n:
        raise AmkParseError(f"Trailing data at offset {i}: {text[i:i+40]!r}")
    if not isinstance(root, dict):
        raise AmkParseError("AMK root node must be an object")
    return root


def parse_amk_file(path: str) -> dict[str, Any]:
    """Read and parse a UTF-16LE .amk script file."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    elif b"\x00" in raw[:64]:
        text = raw.decode("utf-16-le")
    else:
        raise AmkParseError(
            f"Unsupported or binary AMK format in {os.path.basename(path)} "
            "(expected UTF-16LE text AMK VER 248)"
        )
    return parse_amk_text(text)


def decode_xip_image(hex_str: str) -> np.ndarray:
    """Decode an AMK IMG hex string (32-byte XIP TLV header + top-down BGRA) into BGR."""
    if not isinstance(hex_str, str) or not hex_str.startswith("0x"):
        raise AmkParseError(f"Invalid IMG hex string prefix: {str(hex_str)[:20]!r}")
    raw = bytes.fromhex(hex_str[2:])
    if len(raw) < 32:
        raise AmkParseError(f"IMG payload too short: {len(raw)} bytes")
    u0, u1, w, u3, u4, h, tag, datalen = struct.unpack("<6I4sI", raw[:32])
    if (u0, u1, u3, u4) != (87, 4, 72, 4) or tag != b"XIP\x00":
        raise AmkParseError(
            f"Unexpected XIP header: {(u0, u1, w, u3, u4, h, tag, datalen)}"
        )
    expected = w * h * 4
    if datalen != expected or len(raw) - 32 != expected:
        raise AmkParseError(
            f"IMG byte length mismatch: w={w}, h={h}, datalen={datalen}, body={len(raw)-32}"
        )
    bgra = np.frombuffer(raw[32:], dtype=np.uint8).reshape((h, w, 4))
    return bgra[:, :, :3].copy()


def encode_bgr_png_b64(bgr: np.ndarray) -> str:
    """Losslessly encode a BGR numpy array as a base64 PNG string."""
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise AmkParseError("Failed to encode template image to PNG")
    return base64.b64encode(buf.tobytes()).decode("ascii")
