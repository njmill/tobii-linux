#!/usr/bin/env python3
import argparse
import binascii
import math
import selectors
import signal
import socket
import struct
import sys
import time


TTP_OBJECT_NAMES = {
    0x03E8: "hello",
    0x0640: "query_realm",
    0x076C: "open_realm",
    0x04C4: "subscribe",
    0x058C: "device_info",
    0x0532: "metadata_0532",
    0x05D2: "metadata_05d2",
    0x0546: "metadata_0546",
    0x04B0: "stream_enum",
    0x0596: "display_area",
    0x05B4: "metadata_05b4",
    0x06A4: "model_name",
    0x0BF4: "metadata_0bf4",
    0x083E: "session_metadata",
    0x0672: "runtime_metadata_0672",
    0x0C62: "runtime_metadata_0c62",
}


def ascii_preview(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def log_line(path, text):
    line = f"{time.time():.6f} {text}"
    print(line, flush=True)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def ttp_response(seq: int, obj: int, payload: bytes = b"", status: int = 0, kind: int = 1) -> bytes:
    return struct.pack(">IIIIII", 0x52, seq, kind, obj, status, len(payload)) + payload


def ttp_stream_event(seq: int, stream_id: int, payload: bytes = b"") -> bytes:
    return struct.pack(">IIIIII", 0x53, seq, 1, stream_id, 0, len(payload)) + payload


def q42(value: float) -> int:
    return int(round(value * 4398046511104.0))


def fixed16x16(value: float) -> int:
    return int(round(value * 65536.0))


def xds_tlv(tag_type: int, value: bytes) -> bytes:
    return struct.pack(">BI", tag_type, len(value)) + value


def xds_tag(value: int) -> bytes:
    return xds_tlv(0x05, struct.pack(">I", value))


def xds_u32(value: int) -> bytes:
    return xds_tlv(0x02, struct.pack(">I", value))


def xds_i64(value: int) -> bytes:
    return xds_tlv(0x06, struct.pack(">q", value))


def xds_fixed(value: float) -> bytes:
    return xds_tlv(0x03, struct.pack(">i", fixed16x16(value)))


def xds_q42(value: float) -> bytes:
    return xds_tlv(0x04, struct.pack(">q", q42(value)))


def xds_point2d(x: float, y: float) -> bytes:
    return xds_tag(0x021F40) + xds_q42(x) + xds_q42(y)


def xds_point3d(x: float, y: float, z: float) -> bytes:
    return xds_tag(0x031F41) + xds_q42(x) + xds_q42(y) + xds_q42(z)


def xds_column(column_id: int, value: bytes) -> bytes:
    return xds_tag(0x020BB9) + xds_u32(column_id) + value


POSE_PACKET_12D = struct.Struct("=" + "d" * 12)
POSE_PACKET_8D = struct.Struct("=" + "d" * 8)


def clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.5
    return min(1.0, max(0.0, value))


def parse_dashboard_udp_packet(data: bytes) -> dict | None:
    if len(data) == POSE_PACKET_12D.size:
        values = POSE_PACKET_12D.unpack(data)
    elif len(data) == POSE_PACKET_8D.size:
        values = (*POSE_PACKET_8D.unpack(data), 0.5, 0.5, 0.0, 0.0)
    else:
        return None
    return {
        "x_cm": values[0],
        "y_cm": values[1],
        "z_cm": values[2],
        "yaw": values[3],
        "pitch": values[4],
        "roll": values[5],
        "source": values[6],
        "pose_counter": values[7],
        "gaze_x": clamp01(values[8]),
        "gaze_y": clamp01(values[9]),
        "gaze_valid": 1 if math.isfinite(values[10]) and values[10] >= 0.5 else 0,
        "gaze_counter": values[11],
    }


def gaze_payload_from_point(frame_counter: int, elapsed: float, gaze_x: float, gaze_y: float, valid: int) -> bytes:
    gaze_x = clamp01(gaze_x)
    gaze_y = clamp01(gaze_y)
    valid_flag = 1 if valid else 0
    left_x = clamp01(gaze_x - 0.01)
    right_x = clamp01(gaze_x + 0.01)
    eye_z = 635.0
    timestamp_us = int(elapsed * 1000000.0)
    cols = [
        (0x01, xds_i64(timestamp_us)),
        (0x02, xds_point3d(-30.0, 0.0, eye_z)),
        (0x05, xds_point2d(left_x, gaze_y)),
        (0x06, xds_fixed(4.1)),
        (0x07, xds_u32(0)),
        (0x08, xds_point3d(30.0, 0.0, eye_z)),
        (0x0B, xds_point2d(right_x, gaze_y)),
        (0x0C, xds_fixed(4.1)),
        (0x0D, xds_u32(0)),
        (0x14, xds_u32(frame_counter)),
        (0x15, xds_u32(valid_flag)),
        (0x16, xds_u32(valid_flag)),
        (0x1B, xds_u32(valid_flag)),
        (0x1C, xds_point2d(gaze_x, gaze_y)),
        (0x1D, xds_u32(valid_flag)),
        (0x1E, xds_u32(valid_flag)),
        (0x1F, xds_u32(valid_flag)),
        (0x20, xds_point2d(gaze_x, gaze_y)),
        (0x21, xds_u32(valid_flag)),
    ]
    payload = bytearray(b"\x00\x00")
    payload += xds_tag((len(cols) << 16) | 0x0BB8)
    for column_id, value in cols:
        payload += xds_column(column_id, value)
    return bytes(payload)


def synthetic_gaze_payload(frame_counter: int, t0: float) -> bytes:
    elapsed = time.monotonic() - t0
    # A tiny drift makes it easier to tell whether the stock DLL is consuming
    # fresh samples without requiring any live Tobii hardware in the loop.
    gaze_x = 0.50 + 0.10 * math.sin(elapsed * 1.3)
    gaze_y = 0.50 + 0.06 * math.cos(elapsed * 1.1)
    left_x = gaze_x - 0.01
    right_x = gaze_x + 0.01
    eye_z = 635.0
    _ = (left_x, right_x, eye_z)
    return gaze_payload_from_point(frame_counter, elapsed, gaze_x, gaze_y, 1)


PRESENCE_PRESENT_PAYLOAD = bytes.fromhex(
    "0000050000000400020bb8050000000400020bb9020000000400000001"
    "06000000080000002821ac2cdd050000000400020bb9020000000400000002"
    "010000000400000001"
)


def parse_subscribe_stream_id(payload: bytes) -> int | None:
    if len(payload) >= 11 and payload[:8] == bytes.fromhex("0000020000000400"):
        return (payload[9] << 8) | payload[10]

    off = 0
    while off + 8 <= len(payload):
        field_type, field_len = struct.unpack(">II", payload[off : off + 8])
        normalized_len = field_len >> 8 if field_len & 0xFF == 0 and field_len <= 0xFFFF else field_len
        off += 8
        if off + normalized_len > len(payload):
            return None
        value = payload[off : off + normalized_len]
        off += normalized_len
        if field_type in (0x02, 0x0200, 0x20, 0x2000) and normalized_len == 4:
            return struct.unpack(">I", value)[0]
    return None


def tlv_u32(field_type: int, value: int) -> bytes:
    return struct.pack(">IIi", field_type, 4, value)


def tlv_raw(field_type: int, value: bytes) -> bytes:
    return struct.pack(">II", field_type, len(value)) + value


def tlv_string(value: str) -> bytes:
    # Captured TTP metadata payloads use a two-byte payload prefix followed by
    # one-byte string tags. Keeping this byte shape matters: the stock DLL drops
    # the session immediately after device_info if strings are encoded as
    # generic 32-bit TLVs.
    raw = value.encode("ascii")
    return b"\x14" + struct.pack(">II", 4 + len(raw), len(raw)) + raw


def build_device_info_payload(args) -> bytes:
    return (
        b"\x00\x00"
        + tlv_string(args.device_serial)
        + tlv_string(args.model_name)
        + tlv_string(args.short_model)
        + tlv_string(args.firmware)
    )


def parse_display_rect(text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in (text or "").replace("x", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("display rect must be x,y,width,height")
    x, y, w, h = (int(p, 0) for p in parts)
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("display rect width/height must be positive")
    return (x, y, w, h)


def display_geometry(args) -> tuple[int, int, int, int, float, float]:
    if args.display_rect:
        x, y, w, h = args.display_rect
    else:
        x = int(args.display_x)
        y = int(args.display_y)
        w = int(args.display_width)
        h = int(args.display_height)
    if w <= 0:
        w = 6000
    if h <= 0:
        h = 1440
    area_w = float(args.display_area_width if args.display_area_width is not None else w)
    area_h = float(args.display_area_height if args.display_area_height is not None else h)
    return x, y, w, h, area_w, area_h


def build_display_area_payload(args) -> bytes:
    _x, _y, _w, _h, area_w, area_h = display_geometry(args)
    return (
        b"\x00\x00"
        + xds_point3d(-area_w / 2.0, area_h, 0.0)
        + xds_point3d(area_w / 2.0, area_h, 0.0)
        + xds_point3d(-area_w / 2.0, 0.0, 0.0)
        + xds_tag(0x010100)
        + xds_u32(0x3039)
    )


def ttp_payload_tlv_summary(payload: bytes, limit: int = 32) -> str:
    parts = []
    off = 0
    idx = 0
    while off + 8 <= len(payload) and idx < limit:
        field_type, field_len = struct.unpack(">II", payload[off : off + 8])
        off += 8
        if off + field_len > len(payload):
            parts.append(f"field[{idx}] type=0x{field_type:02x} len={field_len} truncated")
            break
        value = payload[off : off + field_len]
        off += field_len
        if field_type in (0x02, 0x03, 0x05, 0x1A) and field_len == 4:
            parts.append(f"field[{idx}] type=0x{field_type:02x} u32=0x{struct.unpack('>I', value)[0]:x}")
        elif field_type == 0x14 and field_len >= 4:
            text_len = struct.unpack(">I", value[:4])[0]
            text = value[4 : 4 + text_len].decode("ascii", errors="replace")
            parts.append(f"field[{idx}] string={text!r}")
        else:
            parts.append(f"field[{idx}] type=0x{field_type:02x} len={field_len} hex={value[:16].hex()}")
        idx += 1
    if off < len(payload):
        parts.append(f"remaining={len(payload) - off}")
    return "; ".join(parts)


def ttp_summary(data: bytes) -> str:
    if len(data) < 24:
        return "ttp=short"
    opcode, seq, kind, obj, status, payload_len = struct.unpack(">IIIIII", data[:24])
    label = TTP_OBJECT_NAMES.get(obj, "unknown")
    return (
        f"ttp op=0x{opcode:04x} seq={seq} kind={kind} "
        f"obj=0x{obj:04x}/{label} status=0x{status:08x} payload_len={payload_len}"
    )


def capability_entry(index: int, enabled: bool) -> bytes:
    text = b"true" if enabled else b"false"
    return (
        bytes.fromhex("0500000004000227100200000004")
        + struct.pack(">I", index)
        + b"\x14"
        + struct.pack(">I", len(text) + 4)
        + struct.pack(">I", len(text))
        + text
    )


def build_capabilities_payload(capability_mode: str = "native", force_headpose: bool = False) -> bytes:
    # Preserve the captured native ET5 byte layout exactly. The TTP object
    # payload encoding is not the same as the outer TTP header, so generated
    # big-endian TLVs will be rejected by the stock DLL.
    native = bytes.fromhex(
        "00000500000004000e0100020000000400002710"
        "05000000040002271002000000040000000014000000080000000474727565"
        "05000000040002271002000000040000000114000000080000000474727565"
        "05000000040002271002000000040000000214000000080000000474727565"
        "05000000040002271002000000040000000314000000090000000566616c7365"
        "05000000040002271002000000040000000414000000090000000566616c7365"
        "05000000040002271002000000040000000514000000080000000474727565"
        "05000000040002271002000000040000000614000000080000000474727565"
        "05000000040002271002000000040000000714000000080000000474727565"
        "05000000040002271002000000040000000814000000080000000474727565"
        "05000000040002271002000000040000000914000000080000000474727565"
        "05000000040002271002000000040000000a14000000090000000566616c7365"
        "05000000040002271002000000040000000b14000000080000000474727565"
        "05000000040002271002000000040000000c14000000090000000566616c7365"
    )
    if capability_mode == "native" and not force_headpose:
        return native

    true_indexes = set()
    false_indexes = set()
    if capability_mode == "all":
        true_indexes.update(range(13))
    elif capability_mode == "headpose":
        true_indexes.add(12)
    elif capability_mode == "host-headpose":
        true_indexes.update((10, 12))
    elif capability_mode != "native":
        raise ValueError(f"unknown capability mode: {capability_mode}")
    if force_headpose:
        true_indexes.add(12)

    patched = native
    for index in range(13):
        false_entry = capability_entry(index, False)
        true_entry = capability_entry(index, True)
        if index in true_indexes:
            patched = patched.replace(false_entry, true_entry)
        elif index in false_indexes:
            patched = patched.replace(true_entry, false_entry)
    return patched


def stream_entry_alias(stream_id: int) -> bytes:
    # Reuse a known-good "gaze" entry shape and change only the stream id.
    # The current hypothesis is that Windows maps support by id, not name.
    return (
        bytes.fromhex("0500000004000413890200000004")
        + struct.pack(">I", stream_id)
        + bytes.fromhex("14000000080000000467617a65140000000400000000020000000400000000")
    )


def build_stream_catalog_payload(variant: str = "native") -> bytes:
    payload = bytes.fromhex(
        "00000500000004000a0100020000000400001389"
        "05000000040004138902000000040000050014000000080000000467617a65"
        "140000000400000000020000000400000000"
        "050000000400041389020000000400000501140000000900000005696d616765"
        "140000000400000000020000000400000000"
        "050000000400041389020000000400000504140000000c0000000870726573656e6365"
        "140000000400000000020000000400000000"
        "050000000400041389020000000400000508140000001400000010696d6167655f636f6c6c656374696f6e"
        "1400000004000000000200000004000003e8"
        "05000000040004138902000000040000050e1400000018000000147072696d6172795f63616d65726163616d6572615f696d616765"
        "140000000400000000020000000400000000"
        "050000000400041389020000000400001770140000000b00000007616c676f646267"
        "140000000400000000020000000400000000"
        "05000000040004138902000000040000177114000000130000000f6973355f73796e635f73747265616d"
        "140000000400000000020000000400000000"
        "0500000004000413890200000004000017721400000007000000036c6f67"
        "140000000400000000020000000400000000"
        "050000000400041389020000000400001774140000000a00000006637573746f6d"
        "140000000400000000020000000400000000"
    )
    # Correct a temporary duplicated substring if this function is edited by hand.
    payload = payload.replace(
        bytes.fromhex("7072696d6172795f63616d65726163616d6572615f696d616765"),
        bytes.fromhex("7072696d6172795f63616d6572615f696d616765"),
    )
    if variant in ("primary-camera", "host-headpose"):
        payload += stream_entry_alias(0x0011)
    if variant == "host-headpose":
        payload += stream_entry_alias(0x0010)
        payload += stream_entry_alias(0x0012)
    return payload


def ttp_minimal_response(data: bytes, args) -> bytes:
    if len(data) < 24:
        return b""
    opcode, seq, _kind, obj, _status, _payload_len = struct.unpack(">IIIIII", data[:24])
    if opcode != 0x51:
        return b""

    if obj == 0x03E8:
        payload = bytes.fromhex("0000020000000400010008")
        return ttp_response(seq, obj, payload)

    if obj == 0x0640:
        payload = bytes.fromhex(
            "0000020000000400000000"
            "020000000400000000"
            "020000000400000000"
        )
        return ttp_response(seq, obj, payload)

    if obj in (0x076C, 0x04C4):
        return ttp_response(seq, obj)

    if obj in (0x0672, 0x0C62):
        # The stock Star Citizen Tobii DLL asks these as no-payload follow-up
        # queries after the core device/display/session metadata. We have not
        # identified their field schema yet, but leaving them unanswered stalls
        # the runtime for several seconds. An empty success response is accepted
        # as a low-risk placeholder while the live stream path is reconstructed.
        if args.runtime_metadata_mode == "drop":
            return b""
        if args.runtime_metadata_mode == "error":
            return ttp_response(seq, obj, status=0x20000504)
        return ttp_response(seq, obj)

    if obj == 0x058C:
        return ttp_response(seq, obj, build_device_info_payload(args))

    force_headpose = args.stream_catalog_variant == "host-headpose" or args.force_headpose_capabilities

    if obj == 0x0546:
        return ttp_response(seq, obj, build_capabilities_payload(args.capability_mode, force_headpose))

    if obj == 0x04B0:
        return ttp_response(seq, obj, build_stream_catalog_payload(args.stream_catalog_variant))

    if obj == 0x0596 and args.display_area_mode == "dynamic":
        payload = build_display_area_payload(args)
        x, y, w, h, area_w, area_h = display_geometry(args)
        log_line(
            args.log,
            f"display_area mode=dynamic rect={x},{y},{w},{h} area={area_w:.1f}x{area_h:.1f} "
            f"display_id={args.display_id} display_name={args.display_name} payload_len={len(payload)}",
        )
        return ttp_response(seq, obj, payload)

    canned_payloads = {
        0x0532: (
            "0000050000000400022710020000000400000000140000000e0000000a5065726970686572616c"
            "05000000040002271002000000040000000114000000080000000474727565"
            "050000000400022710020000000400000002140000000d0000000948454c4c4f5f4e4952"
            "05000000040002271002000000040000000314000000130000000f4953354c455945545241434b455235"
            "05000000040002271002000000040000000514000000090000000566616c7365"
            "05000000040002271002000000040000000614000000090000000566616c7365"
            "05000000040002271002000000040000000714000000090000000566616c7365"
            "05000000040002271002000000040000000c14000000050000000134"
        ),
        0x05D2: (
            "000005000000040002271002000000040000000014000000090000000566616c7365"
            "05000000040002271002000000040000000114000000090000000566616c7365"
            "05000000040002271002000000040000000214000000090000000566616c7365"
            "05000000040002271002000000040000000314000000090000000566616c7365"
            "050000000400022710020000000400000004140000000400000000"
            "0500000004000227100200000004000000051400000006000000026f6b"
            "0500000004000227100200000004000000061400000006000000026f6b"
            "05000000040002271002000000040000000714000000050000000130"
            "05000000040002271002000000040000000814000000090000000566616c7365"
        ),
        0x0596: (
            "0000050000000400031f41"
            "0400000008fff8300000000000"
            "04000000080007d00000000000"
            "04000000080000000000000000"
            "050000000400031f41"
            "04000000080007d00000000000"
            "04000000080007d00000000000"
            "04000000080000000000000000"
            "050000000400031f41"
            "0400000008fff8300000000000"
            "04000000080000000000000000"
            "04000000080000000000000000"
            "050000000400010100"
            "020000000400003039"
        ),
        0x05B4: "0000020000000400000001",
        0x0BF4: "00001a0000000400000000",
        0x083E: (
            "0000020000000400000002"
            "030000000400b80000"
            "030000000400140000"
            "050000000400031f41"
            "04000000080000000000000000"
            "0400000008ffffff5c28f5c290"
            "04000000080000376666666666"
            "050000000400031f41"
            "04000000080000000000000000"
            "0400000008000015851eb851eb"
            "040000000800002770a3d70a3d"
        ),
    }
    if obj in canned_payloads:
        return ttp_response(seq, obj, bytes.fromhex(canned_payloads[obj]))

    if obj == 0x06A4:
        return ttp_response(seq, obj, b"\x00\x00" + tlv_string(args.model_name))

    return b""


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Tobii middleware discovery traffic.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4455)
    parser.add_argument("--udp-port", type=int, default=4457)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--log", default="")
    parser.add_argument("--reply-hex", default="", help="optional fixed response sent after each receive")
    parser.add_argument("--reply-bootstrap", action="store_true", help="reply to minimal TTP startup requests")
    parser.add_argument("--device-serial", default="IS5FF-100203612152")
    parser.add_argument("--model-name", default="IS5_Large_Eyetracker_5")
    parser.add_argument("--short-model", default="IS5")
    parser.add_argument("--firmware", default="02a1a6a977")
    parser.add_argument(
        "--display-id",
        default="DISPLAY\\DEFAULT_MONITOR\\0000&0000",
        help="display device-id hint used for stock DLL display binding diagnostics",
    )
    parser.add_argument("--display-name", default="\\\\.\\DISPLAY1")
    parser.add_argument("--display-x", type=int, default=0)
    parser.add_argument("--display-y", type=int, default=0)
    parser.add_argument("--display-width", type=int, default=6000)
    parser.add_argument("--display-height", type=int, default=1440)
    parser.add_argument("--display-rect", type=parse_display_rect, default=None, help="logical display rect as x,y,width,height")
    parser.add_argument(
        "--display-area-mode",
        choices=("dynamic", "canned"),
        default="dynamic",
        help="0x0596 display_area response mode; canned preserves the older captured blob",
    )
    parser.add_argument("--display-area-width", type=float, default=None)
    parser.add_argument("--display-area-height", type=float, default=None)
    parser.add_argument(
        "--stream-catalog-variant",
        choices=("native", "primary-camera", "host-headpose"),
        default="native",
        help="emulation-only 0x04b0 catalog variant for stock DLL probes",
    )
    parser.add_argument(
        "--force-headpose-capabilities",
        action="store_true",
        help="force 0x0546 capability index 12 true while keeping the stream catalog variant selectable",
    )
    parser.add_argument(
        "--capability-mode",
        choices=("native", "headpose", "host-headpose", "all"),
        default="native",
        help=(
            "0x0546 capability reply mode. 'headpose' preserves the previous index-12 experiment; "
            "'all' marks every observed capability true for provider adoption testing."
        ),
    )
    parser.add_argument(
        "--unsolicited-presence-after-discovery",
        action="store_true",
        help="emit one 0x0504 presence event immediately after model_name discovery, before subscription",
    )
    parser.add_argument("--idle-close-s", type=float, default=5.0)
    parser.add_argument(
        "--runtime-metadata-mode",
        choices=("empty", "drop", "error"),
        default="empty",
        help=(
            "response mode for stock-DLL runtime metadata probes 0x0672/0x0c62; "
            "'drop' intentionally reproduces the pre-adoption stall"
        ),
    )
    parser.add_argument(
        "--synthetic-presence",
        action="store_true",
        help="emit a minimal async 0x0504 presence stream event after a matching subscribe",
    )
    parser.add_argument(
        "--synthetic-gaze",
        action="store_true",
        help="emit synthetic async 0x0500 gaze rows after a matching subscribe",
    )
    parser.add_argument("--synthetic-gaze-hz", type=float, default=30.0)
    parser.add_argument(
        "--live-gaze",
        action="store_true",
        help="emit async 0x0500 gaze rows from dashboard UDP packets instead of synthetic gaze",
    )
    parser.add_argument("--live-gaze-hz", type=float, default=60.0)
    parser.add_argument("--live-gaze-max-age-s", type=float, default=0.5)
    args = parser.parse_args()

    reply = b""
    if args.reply_hex:
        reply = binascii.unhexlify("".join(args.reply_hex.split()))

    stop = False

    def on_signal(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    sel = selectors.DefaultSelector()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(16)
    server.setblocking(False)
    sel.register(server, selectors.EVENT_READ, ("server", None))

    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_server.bind((args.host, args.udp_port))
    udp_server.setblocking(False)
    sel.register(udp_server, selectors.EVENT_READ, ("udp", None))

    clients = {}
    live_gaze = {
        "seen": 0,
        "last": 0.0,
        "x": 0.5,
        "y": 0.5,
        "valid": 0,
        "counter": 0.0,
        "source": 0.0,
    }
    deadline = time.monotonic() + args.seconds
    log_line(
        args.log,
        f"listening host={args.host} port={args.port} udp_port={args.udp_port} "
        f"seconds={args.seconds:.1f} synthetic_presence={int(args.synthetic_presence)} "
        f"synthetic_gaze={int(args.synthetic_gaze)} live_gaze={int(args.live_gaze)} "
        f"runtime_metadata_mode={args.runtime_metadata_mode} capability_mode={args.capability_mode} "
        f"unsolicited_presence_after_discovery={int(args.unsolicited_presence_after_discovery)} "
        f"display_area_mode={args.display_area_mode} display_rect={display_geometry(args)[:4]} "
        f"display_id={args.display_id} display_name={args.display_name}",
    )

    try:
        while not stop and time.monotonic() < deadline:
            select_timeout = 0.2
            if (args.synthetic_gaze or args.live_gaze) and clients:
                hz = args.live_gaze_hz if args.live_gaze else args.synthetic_gaze_hz
                select_timeout = min(select_timeout, 1.0 / max(hz, 0.1))
            for key, _mask in sel.select(timeout=select_timeout):
                kind, cid = key.data
                if kind == "server":
                    conn, addr = server.accept()
                    conn.setblocking(False)
                    cid = id(conn)
                    clients[cid] = {
                        "socket": conn,
                        "addr": addr,
                        "opened": time.monotonic(),
                        "last": time.monotonic(),
                        "rx": 0,
                        "tx": 0,
                        "events": [],
                        "stream_subscriptions": set(),
                        "event_seq": 0x1000,
                        "synthetic_frame_counter": 1,
                        "synthetic_start": time.monotonic(),
                        "next_gaze_event": 0.0,
                    }
                    sel.register(conn, selectors.EVENT_READ, ("client", cid))
                    log_line(args.log, f"accept cid={cid} peer={addr[0]}:{addr[1]}")
                    continue

                if kind == "udp":
                    data, addr = udp_server.recvfrom(65536)
                    packet = parse_dashboard_udp_packet(data)
                    if packet:
                        live_gaze.update(
                            {
                                "seen": live_gaze["seen"] + 1,
                                "last": time.monotonic(),
                                "x": packet["gaze_x"],
                                "y": packet["gaze_y"],
                                "valid": packet["gaze_valid"],
                                "counter": packet["gaze_counter"],
                                "source": packet["source"],
                            }
                        )
                        if live_gaze["seen"] <= 5 or live_gaze["seen"] % 120 == 0:
                            log_line(
                                args.log,
                                "live_gaze_udp "
                                f"packets={live_gaze['seen']} peer={addr[0]}:{addr[1]} "
                                f"x={packet['gaze_x']:.4f} y={packet['gaze_y']:.4f} valid={packet['gaze_valid']} "
                                f"counter={packet['gaze_counter']:.0f} source={packet['source']:.1f}",
                            )
                    else:
                        log_line(
                            args.log,
                            f"udp_recv peer={addr[0]}:{addr[1]} bytes={len(data)} hex={data.hex()} ascii={ascii_preview(data)!r}",
                        )
                    continue

                client = clients.get(cid)
                if not client:
                    continue
                conn = client["socket"]
                try:
                    data = conn.recv(65536)
                except ConnectionResetError:
                    data = b""
                if not data:
                    log_line(
                        args.log,
                        f"close cid={cid} rx={client['rx']} tx={client['tx']} age={time.monotonic() - client['opened']:.3f}s",
                    )
                    sel.unregister(conn)
                    conn.close()
                    clients.pop(cid, None)
                    continue

                client["last"] = time.monotonic()
                client["rx"] += len(data)
                log_line(
                    args.log,
                    f"recv cid={cid} bytes={len(data)} {ttp_summary(data)} hex={data.hex()} ascii={ascii_preview(data)!r}",
                )
                out = reply
                if args.reply_bootstrap:
                    out = ttp_minimal_response(data, args) or out
                if out:
                    conn.sendall(out)
                    client["tx"] += len(out)
                    payload = out[24:] if len(out) >= 24 else b""
                    tlv = ttp_payload_tlv_summary(payload) if payload else ""
                    suffix = f" tlv={tlv}" if tlv else ""
                    log_line(args.log, f"send cid={cid} bytes={len(out)} {ttp_summary(out)}{suffix} hex={out.hex()}")

                if len(data) >= 24:
                    opcode, _seq, _kind, obj, _status, payload_len = struct.unpack(">IIIIII", data[:24])
                    payload = data[24 : 24 + payload_len]
                    stream_id = parse_subscribe_stream_id(payload) if obj == 0x04C4 else None
                    if obj == 0x04C4:
                        log_line(
                            args.log,
                            f"subscribe_parse cid={cid} synthetic_presence={int(args.synthetic_presence)} "
                            f"synthetic_gaze={int(args.synthetic_gaze)} live_gaze={int(args.live_gaze)} "
                            f"opcode=0x{opcode:04x} payload_len={payload_len} "
                            f"actual_payload_len={len(payload)} stream={stream_id}",
                        )
                        if stream_id is not None:
                            client["stream_subscriptions"].add(stream_id)
                            log_line(args.log, f"adoption_phase=tcp_subscribe cid={cid} stream=0x{stream_id:04x}")
                    if opcode == 0x51 and obj in (0x0672, 0x0C62):
                        log_line(
                            args.log,
                            f"adoption_phase=runtime_metadata cid={cid} obj=0x{obj:04x} mode={args.runtime_metadata_mode}",
                        )
                    if args.unsolicited_presence_after_discovery and opcode == 0x51 and obj == 0x06A4:
                        client["events"].append((time.monotonic() + 0.02, 0x0504))
                        log_line(args.log, f"schedule_unsolicited_presence cid={cid} stream=0x0504 after=0x06a4")
                    if args.synthetic_presence and opcode == 0x51 and stream_id == 0x0504:
                        client["events"].append((time.monotonic() + 0.05, stream_id))
                        log_line(args.log, f"schedule_stream_event cid={cid} stream=0x{stream_id:04x}")
                    if args.synthetic_gaze and opcode == 0x51 and stream_id == 0x0500:
                        client["next_gaze_event"] = time.monotonic() + 0.05
                        log_line(args.log, f"schedule_gaze_stream cid={cid} stream=0x{stream_id:04x} hz={args.synthetic_gaze_hz:.1f}")
                    if args.live_gaze and opcode == 0x51 and stream_id == 0x0500:
                        client["next_gaze_event"] = time.monotonic() + 0.02
                        log_line(args.log, f"schedule_live_gaze_stream cid={cid} stream=0x{stream_id:04x} hz={args.live_gaze_hz:.1f}")

            now = time.monotonic()
            if args.live_gaze:
                period = 1.0 / max(args.live_gaze_hz, 0.1)
                for cid, client in list(clients.items()):
                    if 0x0500 not in client["stream_subscriptions"] or now < client["next_gaze_event"]:
                        continue
                    event_seq = client["event_seq"]
                    client["event_seq"] += 1
                    frame_counter = client["synthetic_frame_counter"]
                    client["synthetic_frame_counter"] += 1
                    age = now - float(live_gaze["last"]) if live_gaze["last"] else 9999.0
                    valid = int(live_gaze["valid"]) if age <= args.live_gaze_max_age_s else 0
                    payload = gaze_payload_from_point(
                        frame_counter,
                        now - client["synthetic_start"],
                        float(live_gaze["x"]),
                        float(live_gaze["y"]),
                        valid,
                    )
                    packet = ttp_stream_event(event_seq, 0x0500, payload)
                    try:
                        client["socket"].sendall(packet)
                    except OSError as exc:
                        log_line(args.log, f"live_gaze_event_send_error cid={cid} stream=0x0500 error={exc}")
                        continue
                    client["tx"] += len(packet)
                    client["last"] = now
                    client["next_gaze_event"] = now + period
                    log_line(
                        args.log,
                        f"send_live_gaze_event cid={cid} frame={frame_counter} bytes={len(packet)} "
                        f"x={float(live_gaze['x']):.4f} y={float(live_gaze['y']):.4f} valid={valid} age={age:.3f}s "
                        f"{ttp_summary(packet)} payload_len={len(payload)}",
                    )
                    if frame_counter == 1:
                        log_line(args.log, f"adoption_phase=gaze_event cid={cid} stream=0x0500")
            elif args.synthetic_gaze:
                period = 1.0 / max(args.synthetic_gaze_hz, 0.1)
                for cid, client in list(clients.items()):
                    if 0x0500 not in client["stream_subscriptions"] or now < client["next_gaze_event"]:
                        continue
                    event_seq = client["event_seq"]
                    client["event_seq"] += 1
                    frame_counter = client["synthetic_frame_counter"]
                    client["synthetic_frame_counter"] += 1
                    payload = synthetic_gaze_payload(frame_counter, client["synthetic_start"])
                    packet = ttp_stream_event(event_seq, 0x0500, payload)
                    try:
                        client["socket"].sendall(packet)
                    except OSError as exc:
                        log_line(args.log, f"gaze_event_send_error cid={cid} stream=0x0500 error={exc}")
                        continue
                    client["tx"] += len(packet)
                    client["last"] = now
                    client["next_gaze_event"] = now + period
                    log_line(
                        args.log,
                        f"send_gaze_event cid={cid} frame={frame_counter} bytes={len(packet)} "
                        f"{ttp_summary(packet)} payload_len={len(payload)}",
                    )
                    if frame_counter == 1:
                        log_line(args.log, f"adoption_phase=gaze_event cid={cid} stream=0x0500")

            for cid, client in list(clients.items()):
                due = [event for event in client["events"] if event[0] <= now]
                if not due:
                    continue
                client["events"] = [event for event in client["events"] if event[0] > now]
                conn = client["socket"]
                for _event_time, stream_id in due:
                    event_seq = client["event_seq"]
                    client["event_seq"] += 1
                    payload = PRESENCE_PRESENT_PAYLOAD if stream_id == 0x0504 else b""
                    packet = ttp_stream_event(event_seq, stream_id, payload)
                    try:
                        conn.sendall(packet)
                    except OSError as exc:
                        log_line(args.log, f"stream_event_send_error cid={cid} stream=0x{stream_id:04x} error={exc}")
                        continue
                    client["tx"] += len(packet)
                    client["last"] = now
                    log_line(
                        args.log,
                        f"send_stream_event cid={cid} bytes={len(packet)} {ttp_summary(packet)} hex={packet.hex()}",
                    )
                    log_line(args.log, f"adoption_phase=stream_event cid={cid} stream=0x{stream_id:04x}")

            for cid, client in list(clients.items()):
                if now - client["last"] > args.idle_close_s:
                    conn = client["socket"]
                    log_line(
                        args.log,
                        f"idle_close cid={cid} rx={client['rx']} tx={client['tx']} idle={now - client['last']:.3f}s",
                    )
                    try:
                        sel.unregister(conn)
                    except Exception:
                        pass
                    conn.close()
                    clients.pop(cid, None)
    finally:
        for client in list(clients.values()):
            try:
                sel.unregister(client["socket"])
            except Exception:
                pass
            client["socket"].close()
        sel.close()
        server.close()
        udp_server.close()
        log_line(args.log, "stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
