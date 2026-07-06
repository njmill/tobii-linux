#!/usr/bin/env python3
import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path


OBJ_RE = re.compile(r"obj=0x([0-9a-fA-F]{4})/([a-zA-Z0-9_]+)")
STREAM_RE = re.compile(r"stream=(0x[0-9a-fA-F]+|\d+)")
CID_RE = re.compile(r"\bcid=(\d+)")
RXTX_RE = re.compile(r"\brx=(\d+) tx=(\d+)")
SESP_TYPE_RE = re.compile(r"sesp_frame_decode seq=\d+ type=(\d+)")
TIME_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+")
ATTR_RE = re.compile(r'<Attr\s+name="([^"]+)"\s+value="([^"]*)"')
PACKETS_RE = re.compile(r"packets=(\d+)")
ADOPTION_KICK_MARKER_RE = re.compile(r"adoption_kick_marker\s+(start|end)\s+pid=(\d+)(?:\s+rc=(-?\d+))?")
KICK_MARKER_RE = re.compile(r"kick_marker\s+name=([^\s]+)\s+phase=(start|end)\s+pid=(\d+)(?:\s+rc=(-?\d+))?")


OBJECT_NAMES = {
    0x03E8: "hello",
    0x0640: "query_realm",
    0x076C: "open_realm",
    0x04C4: "subscribe",
    0x058C: "device_info",
    0x0532: "metadata_0532",
    0x05D2: "metadata_05d2",
    0x0546: "capabilities",
    0x04B0: "stream_enum",
    0x0596: "display_area",
    0x05B4: "metadata_05b4",
    0x06A4: "model_name",
    0x0BF4: "metadata_0bf4",
    0x083E: "session_metadata",
    0x0672: "runtime_metadata_0672",
    0x0C62: "runtime_metadata_0c62",
}

STREAM_NAMES = {
    0x0500: "gaze",
    0x0501: "image",
    0x0504: "presence",
    0x0508: "image_collection",
    0x050E: "primary_camera_image",
    0x1770: "algodbg",
    0x1771: "is5_sync_stream",
    0x0010: "head_position_alias",
    0x0011: "primary_camera_alias",
    0x0012: "gaze_alias",
}

HEADTRACKING_SOURCES = {
    "0": "None",
    "1": "NaturalPoint TrackIR",
    "2": "Faceware",
    "3": "Tobii",
    "4": "Debug device",
    "5": "HMD VR",
}

API_TRACE_SYMBOLS = (
    "GetApi",
    "IsPresent",
    "IsConnected",
    "IsDeviceEnabled",
    "Update",
    "TrackWindow",
    "TrackRectangle",
    "TrackTracker",
    "GetTrackerInfos",
    "GetTrackerInfo",
    "GetTrackerInfoByUrl",
    "UpdateTrackerInfos",
    "GetLatestHeadPose",
    "GetLatestGazePoint",
    "IsStreamSupported",
    "TrackHMD",
)


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def read_attrs(path: Path) -> dict[str, str]:
    attrs = {}
    for line in read_lines(path):
        match = ATTR_RE.search(line)
        if match:
            attrs[match.group(1)] = match.group(2)
    return attrs


def line_time(line: str) -> float | None:
    match = TIME_RE.match(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_tcp(lines: list[str]) -> dict:
    adoption_intervals = []
    kick_intervals = []
    open_kicks = {}
    open_named_kicks = {}
    for line in lines:
        ts = line_time(line)
        if ts is None:
            continue
        named_match = KICK_MARKER_RE.search(line)
        if named_match:
            name, phase, pid, rc = named_match.groups()
            if phase == "start":
                open_named_kicks[pid] = {"name": name, "start": ts}
            else:
                start_info = open_named_kicks.pop(pid, {"name": name, "start": None})
                interval = {"name": start_info.get("name") or name, "pid": pid, "start": start_info.get("start"), "end": ts, "rc": rc}
                kick_intervals.append(interval)
                if interval["name"] == "adoption-kick":
                    adoption_intervals.append(interval)
            continue
        match = ADOPTION_KICK_MARKER_RE.search(line)
        if match:
            phase, pid, rc = match.groups()
            if phase == "start":
                open_kicks[pid] = ts
            else:
                interval = {"name": "adoption-kick", "pid": pid, "start": open_kicks.pop(pid, None), "end": ts, "rc": rc}
                adoption_intervals.append(interval)
                kick_intervals.append(interval)
    for pid, start in open_kicks.items():
        interval = {"name": "adoption-kick", "pid": pid, "start": start, "end": None, "rc": None}
        adoption_intervals.append(interval)
        kick_intervals.append(interval)
    for pid, start_info in open_named_kicks.items():
        interval = {"name": start_info.get("name", "unknown-kick"), "pid": pid, "start": start_info.get("start"), "end": None, "rc": None}
        kick_intervals.append(interval)

    def dedupe_intervals(intervals: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for interval in intervals:
            key = (
                interval.get("name"),
                interval.get("pid"),
                interval.get("start"),
                interval.get("end"),
                interval.get("rc"),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(interval)
        return out

    adoption_intervals = dedupe_intervals(adoption_intervals)
    kick_intervals = dedupe_intervals(kick_intervals)

    def session_origin(ts: float | None) -> str:
        if ts is None:
            return "unknown"
        for interval in kick_intervals:
            start = interval.get("start")
            end = interval.get("end")
            if start is None:
                continue
            if ts >= start and (end is None or ts <= end + 0.250):
                return str(interval.get("name") or "unknown-kick")
        return "sc-or-unknown"

    sessions: dict[str, dict] = {}
    active_by_cid: dict[str, str] = {}
    objects = Counter()
    subscriptions = Counter()
    stream_events = Counter()
    gaze_events = 0
    live_gaze_events = 0
    live_gaze_event_valid = 0
    live_gaze_udp = 0
    live_gaze_valid = 0
    empty_connects = 0

    first_times = {}
    for line in lines:
        ts = line_time(line)
        cid_match = CID_RE.search(line)
        cid = cid_match.group(1) if cid_match else None
        if " accept " in line and cid:
            sid = f"{cid}.{len(sessions) + 1}"
            active_by_cid[cid] = sid
            sessions[sid] = {
                "rx": None,
                "tx": None,
                "start": ts,
                "end": None,
                "objects": [],
                "object_times": [],
                "subscriptions": [],
                "subscription_times": [],
                "stream_events": Counter(),
                "gaze_events": 0,
                "first_gaze_event": None,
                "first_stream_event": None,
                "origin": session_origin(ts),
            }
        if "recv " in line:
            obj_match = OBJ_RE.search(line)
            if obj_match:
                obj = int(obj_match.group(1), 16)
                objects[obj] += 1
                first_times.setdefault(("object", obj), ts)
                if cid:
                    sid = active_by_cid.get(cid, cid)
                    sessions.setdefault(sid, {"rx": None, "tx": None, "objects": [], "subscriptions": []})
                    sessions[sid]["objects"].append(obj)
                    sessions[sid].setdefault("object_times", []).append(ts)
        if "subscribe_parse" in line:
            stream_match = STREAM_RE.search(line)
            if stream_match:
                text = stream_match.group(1)
                stream = int(text, 16 if text.startswith("0x") else 10)
                subscriptions[stream] += 1
                first_times.setdefault(("subscription", stream), ts)
                if cid:
                    sid = active_by_cid.get(cid, cid)
                    sessions.setdefault(sid, {"rx": None, "tx": None, "objects": [], "subscriptions": []})
                    sessions[sid]["subscriptions"].append(stream)
                    sessions[sid].setdefault("subscription_times", []).append(ts)
        if "send_stream_event" in line:
            obj_match = OBJ_RE.search(line)
            if obj_match:
                stream = int(obj_match.group(1), 16)
                stream_events[stream] += 1
                first_times.setdefault(("stream_event", stream), ts)
                if cid:
                    sid = active_by_cid.get(cid, cid)
                    session = sessions.get(sid)
                    if session is not None:
                        session.setdefault("stream_events", Counter())[stream] += 1
                        session.setdefault("first_stream_event", ts)
        if "live_gaze_udp" in line:
            packets_match = PACKETS_RE.search(line)
            if packets_match:
                live_gaze_udp = max(live_gaze_udp, int(packets_match.group(1)))
            else:
                live_gaze_udp += 1
            if "valid=1" in line:
                live_gaze_valid = max(live_gaze_valid, live_gaze_udp)
            first_times.setdefault("live_gaze_udp", ts)
        if "send_live_gaze_event" in line:
            live_gaze_events += 1
            gaze_events += 1
            if "valid=1" in line:
                live_gaze_event_valid += 1
            first_times.setdefault(("gaze_event", 0x0500), ts)
            first_times.setdefault("live_gaze_event", ts)
            if cid:
                sid = active_by_cid.get(cid, cid)
                session = sessions.get(sid)
                if session is not None:
                    session["gaze_events"] = session.get("gaze_events", 0) + 1
                    session["live_gaze_events"] = session.get("live_gaze_events", 0) + 1
                    if session.get("first_gaze_event") is None:
                        session["first_gaze_event"] = ts
        elif "send_gaze_event" in line:
            gaze_events += 1
            first_times.setdefault(("gaze_event", 0x0500), ts)
            if cid:
                sid = active_by_cid.get(cid, cid)
                session = sessions.get(sid)
                if session is not None:
                    session["gaze_events"] = session.get("gaze_events", 0) + 1
                    if session.get("first_gaze_event") is None:
                        session["first_gaze_event"] = ts
        if " close " in line:
            counts = RXTX_RE.search(line)
            if counts:
                rx = int(counts.group(1))
                tx = int(counts.group(2))
                if cid:
                    sid = active_by_cid.get(cid, cid)
                    sessions.setdefault(sid, {"rx": None, "tx": None, "objects": [], "subscriptions": []})
                    sessions[sid]["rx"] = rx
                    sessions[sid]["tx"] = tx
                    sessions[sid]["end"] = ts
                    active_by_cid.pop(cid, None)
                if rx == 0 and tx == 0:
                    empty_connects += 1

    non_empty = [s for s in sessions.values() if s.get("rx") or s.get("objects")]
    richest = max(non_empty, key=lambda s: len(s["objects"]), default=None)
    return {
        "sessions": sessions,
        "non_empty_sessions": non_empty,
        "richest": richest,
        "objects": objects,
        "subscriptions": subscriptions,
        "stream_events": stream_events,
        "gaze_events": gaze_events,
        "live_gaze_events": live_gaze_events,
        "live_gaze_event_valid": live_gaze_event_valid,
        "live_gaze_udp": live_gaze_udp,
        "live_gaze_valid": live_gaze_valid,
        "empty_connects": empty_connects,
        "first_times": first_times,
        "adoption_intervals": adoption_intervals,
        "kick_intervals": kick_intervals,
    }


def parse_kick_log(path: Path) -> dict:
    lines = read_lines(path)
    text = "\n".join(lines)
    updates = len(re.findall(r"^Update\[\d+\]=", text, re.MULTILINE))
    headpose_ok = len(re.findall(r"^GetLatestHeadPose\[\d+\]=1\b", text, re.MULTILINE))
    gaze_ok = len(re.findall(r"^GetLatestGazePoint\[\d+\]=1\b", text, re.MULTILINE))
    present_match = re.search(r"^IsPresent=(-?\d+)\b", text, re.MULTILINE)
    connected_match = re.search(r"^IsConnected=(-?\d+)\b", text, re.MULTILINE)
    enabled_match = re.search(r"^IsDeviceEnabled=(-?\d+)\b", text, re.MULTILINE)
    trackwindow_match = re.search(r"^TrackWindow rc=(-?\d+)\b", text, re.MULTILINE)
    tracktracker_match = re.search(r"^TrackTracker rc=(-?\d+)\b", text, re.MULTILINE)
    seed_match = re.search(
        r"^diagnostic_seed_device_table\b.*\bactive_bank=(\d+)\b.*\bflags=(\d+),(\d+)\b.*\bprev_flags=(\d+),(\d+)\b.*\brect=([-\d]+),([-\d]+),([-\d]+),([-\d]+)\b.*\bstatus=0x([0-9a-fA-F]+)\b.*\btype=(\d+)\b.*\bcaps=0x([0-9a-fA-F]+)\b",
        text,
        re.MULTILINE,
    )
    selector_match = re.search(
        r"^diagnostic_force_provider_select\b.*\brect=([-\d]+),([-\d]+),([-\d]+),([-\d]+)\b.*\bselector_index=0x([0-9a-fA-F]+)\b.*\bindex=(\d+)\b.*\bmask=0x([0-9a-fA-F]+)\b",
        text,
        re.MULTILINE,
    )
    window_selector_matches = list(
        re.finditer(
            r"^diagnostic_window_selector\b.*\bphase=([^\s]+)\b(?:.*\bdestructive=(\d+)\b)?.*\bcount=(\d+)\b.*\bbank=(\d+)\b.*\bflags0=(\d+)\b.*\bflags1=(\d+)\b.*\brect=([-\d]+),([-\d]+),([-\d]+),([-\d]+)\b.*\bselector_index=0x([0-9a-fA-F]+)\b(?:.*\bmanual_selector_index=0x([0-9a-fA-F]+)\b)?",
            text,
            re.MULTILINE,
        )
    )
    connector_entry_matches = list(
        re.finditer(
            r"^diagnostic_connector_entry\b.*\bphase=([^\s]+)\b.*\bindex=(\d+)\b.*\bflag_active=(\d+)\b.*\bflag_other=(\d+)\b.*\bstatus=0x([0-9a-fA-F]+)\b.*\btype=(\d+)\b.*\bcaps=0x([0-9a-fA-F]+)\b.*\brect=([-\d]+),([-\d]+),([-\d]+),([-\d]+)\b.*\burl=(.*)$",
            text,
            re.MULTILINE,
        )
    )
    connector_gate_matches = list(
        re.finditer(
            r"^diagnostic_connector_gates\b.*\bphase=([^\s]+)\b.*\bcurrent_index=0x([0-9a-fA-F]+)\b.*\bcount=(\d+)\b.*\bbank=(\d+)\b.*\bflags=(\d+),(\d+),(\d+),(\d+)\b.*\bstate=(-?\d+)\b.*\bmask=0x([0-9a-fA-F]+)\b.*\bstatus0=(-?\d+)",
            text,
            re.MULTILINE,
        )
    )
    track_window_identity_matches = list(
        re.finditer(
            r"^diagnostic_track_window_identity\b.*\bphase=([^\s]+)\b.*\bhwnd=([^\s]+)\b.*\bpid=(\d+)\b.*\btid=(\d+)\b.*\bvisible=(\d+)\b.*\brect=([-\d]+),([-\d]+),([-\d]+),([-\d]+)\b.*\bclient=([-\d]+),([-\d]+),([-\d]+),([-\d]+)\b.*\bmonitor=([-\d]+),([-\d]+),([-\d]+),([-\d]+)\b.*\bmonitor_name=([^\s]*)\b.*\bclass=([^\s]*)\b.*\btitle=(.*)$",
            text,
            re.MULTILINE,
        )
    )
    window_selector_match = window_selector_matches[-1] if window_selector_matches else None
    effective_selector_match = selector_match or window_selector_match
    provider_select_match = re.search(r"^diagnostic_force_provider_select rc=(-?\d+)\b", text, re.MULTILINE)
    provider_subscribe_match = re.search(r"^diagnostic_force_provider_subscribe rc=(-?\d+)\b", text, re.MULTILINE)
    force_connected_match = re.search(
        r"^diagnostic_force_provider_connected_fields new20=(\d+) new21=(\d+) new22=(\d+) new24=(\d+) new28=0x([0-9a-fA-F]+) new2c=0x([0-9a-fA-F]+)\b",
        text,
        re.MULTILINE,
    )
    stream_supported = {
        int(match.group(1), 16): int(match.group(2))
        for match in re.finditer(r"^IsStreamSupported(?:\[\d+\])?\[(0x[0-9a-fA-F]+)\]=(-?\d+)\b", text, re.MULTILINE)
    }
    connected_observed = bool(
        re.search(r"^IsConnected(?:_[A-Za-z0-9_]+|\[\d+\])?=1\b", text, re.MULTILINE)
    )
    provider_connected_observed = bool(
        re.search(r"^connected_probe provider_state=2\b.*\bstatus0=1\b", text, re.MULTILINE)
    )
    return {
        "exists": path.exists(),
        "present": int(present_match.group(1)) if present_match else None,
        "connected_initial": int(connected_match.group(1)) if connected_match else None,
        "enabled": int(enabled_match.group(1)) if enabled_match else None,
        "stream_supported": stream_supported,
        "tracktracker_called": bool(re.search(r"^call TrackTracker$", text, re.MULTILINE)),
        "tracktracker_rc": int(tracktracker_match.group(1)) if tracktracker_match else None,
        "tracktracker_ok": "TrackTracker rc=1" in text,
        "trackwindow_called": bool(re.search(r"^call TrackWindow$", text, re.MULTILINE)),
        "trackwindow_rc": int(trackwindow_match.group(1)) if trackwindow_match else None,
        "trackwindow_ok": int(trackwindow_match.group(1)) == 0 if trackwindow_match else False,
        "sc_vtable_sequence": "sc_vtable_sequence=begin" in text,
        "seeded_device_table": bool(seed_match),
        "seed": {
            "active_bank": int(seed_match.group(1)) if seed_match else None,
            "flags": (int(seed_match.group(2)), int(seed_match.group(3))) if seed_match else None,
            "previous_flags": (int(seed_match.group(4)), int(seed_match.group(5))) if seed_match else None,
            "rect": tuple(int(seed_match.group(i)) for i in range(6, 10)) if seed_match else None,
            "status": int(seed_match.group(10), 16) if seed_match else None,
            "type": int(seed_match.group(11)) if seed_match else None,
            "caps": int(seed_match.group(12), 16) if seed_match else None,
        },
        "selector_index": (
            int(selector_match.group(5), 16)
            if selector_match
            else int(window_selector_match.group(11), 16) if window_selector_match else None
        ),
        "selector_forced_index": int(selector_match.group(6)) if selector_match else None,
        "selector_mask": int(selector_match.group(7), 16) if selector_match else None,
        "selector_rect": (
            tuple(int(selector_match.group(i)) for i in range(1, 5))
            if selector_match
            else tuple(int(window_selector_match.group(i)) for i in range(7, 11)) if window_selector_match else None
        ),
        "selector_probe_only": bool(window_selector_match and not selector_match),
        "window_selectors": [
            {
                "phase": match.group(1),
                "destructive": bool(int(match.group(2) or "1")),
                "count": int(match.group(3)),
                "bank": int(match.group(4)),
                "flags": (int(match.group(5)), int(match.group(6))),
                "rect": tuple(int(match.group(i)) for i in range(7, 11)),
                "selector_index": int(match.group(11), 16),
                "manual_selector_index": int(match.group(12), 16) if match.group(12) else None,
            }
            for match in window_selector_matches
        ],
        "connector_gates": [
            {
                "phase": match.group(1),
                "current_index": int(match.group(2), 16),
                "count": int(match.group(3)),
                "bank": int(match.group(4)),
                "flags": tuple(int(match.group(i)) for i in range(5, 9)),
                "state": int(match.group(9)),
                "mask": int(match.group(10), 16),
                "status0": int(match.group(11)),
            }
            for match in connector_gate_matches
        ],
        "connector_entries": [
            {
                "phase": match.group(1),
                "index": int(match.group(2)),
                "flag_active": int(match.group(3)),
                "flag_other": int(match.group(4)),
                "status": int(match.group(5), 16),
                "type": int(match.group(6)),
                "caps": int(match.group(7), 16),
                "rect": tuple(int(match.group(i)) for i in range(8, 12)),
                "url": match.group(12).strip(),
            }
            for match in connector_entry_matches
        ],
        "track_window_identities": [
            {
                "phase": match.group(1),
                "hwnd": match.group(2),
                "pid": int(match.group(3)),
                "tid": int(match.group(4)),
                "visible": bool(int(match.group(5))),
                "rect": tuple(int(match.group(i)) for i in range(6, 10)),
                "client": tuple(int(match.group(i)) for i in range(10, 14)),
                "monitor": tuple(int(match.group(i)) for i in range(14, 18)),
                "monitor_name": match.group(18),
                "class": match.group(19),
                "title": match.group(20),
            }
            for match in track_window_identity_matches
        ],
        "provider_select_rc": int(provider_select_match.group(1)) if provider_select_match else None,
        "provider_subscribe_rc": int(provider_subscribe_match.group(1)) if provider_subscribe_match else None,
        "forced_provider_connected": bool(force_connected_match),
        "forced_provider_state": {
            "byte20": int(force_connected_match.group(1)) if force_connected_match else None,
            "byte21": int(force_connected_match.group(2)) if force_connected_match else None,
            "byte22": int(force_connected_match.group(3)) if force_connected_match else None,
            "state": int(force_connected_match.group(4)) if force_connected_match else None,
            "status_a": int(force_connected_match.group(5), 16) if force_connected_match else None,
            "status_b": int(force_connected_match.group(6), 16) if force_connected_match else None,
        },
        "connected": connected_observed,
        "provider_connected": provider_connected_observed,
        "updates": updates,
        "headpose_ok": headpose_ok,
        "gaze_ok": gaze_ok,
    }


def parse_adoption_kick(path: Path) -> dict:
    return parse_kick_log(path)


def summarize_sessions(sessions: list[dict]) -> dict:
    objects = Counter()
    subscriptions = Counter()
    stream_events = Counter()
    gaze_events = 0
    live_gaze_events = 0
    for session in sessions:
        objects.update(session.get("objects", []))
        subscriptions.update(session.get("subscriptions", []))
        stream_events.update(session.get("stream_events", Counter()))
        gaze_events += int(session.get("gaze_events", 0) or 0)
        live_gaze_events += int(session.get("live_gaze_events", 0) or 0)
    richest = max(sessions, key=lambda s: len(s.get("objects", [])), default=None)
    return {
        "objects": objects,
        "subscriptions": subscriptions,
        "stream_events": stream_events,
        "gaze_events": gaze_events,
        "live_gaze_events": live_gaze_events,
        "richest": richest,
    }


def parse_pipe(lines: list[str]) -> dict:
    counts = Counter()
    live_sources = Counter()
    request_types = Counter()
    first_times = {}
    for line in lines:
        ts = line_time(line)
        for key in (
            "pipe_accept",
            "sesp_request",
            "sesp_response",
            "sesp_synthetic_headpose_start",
            "sesp_synthetic_headpose",
            "live_pose_udp_packet",
        ):
            if key in line:
                counts[key] += 1
                first_times.setdefault(key, ts)
        if "client_pipe_sesp_provider_nudge " in line:
            counts["sesp_provider_nudge_ok"] += 1
            first_times.setdefault("sesp_provider_nudge_ok", ts)
        if "client_pipe_sesp_provider_nudge_failed" in line:
            counts["sesp_provider_nudge_failed"] += 1
            first_times.setdefault("sesp_provider_nudge_failed", ts)
        if "sesp_provider_nudge after_display_info" in line:
            counts["sesp_provider_nudge_attempt"] += 1
            first_times.setdefault("sesp_provider_nudge_attempt", ts)
        match = SESP_TYPE_RE.search(line)
        if match:
            req_type = int(match.group(1))
            request_types[req_type] += 1
            first_times.setdefault(("sesp_type", req_type), ts)
        if "live_pose_udp_packet" in line:
            match = re.search(r"source=(\d+)", line)
            if match:
                live_sources[int(match.group(1))] += 1
    return {"counts": counts, "live_sources": live_sources, "request_types": request_types, "first_times": first_times}


def parse_wine_window_probe(lines: list[str]) -> dict:
    windows = []

    def parse_rect_text(text: str | None) -> tuple[int, int, int, int] | None:
        if not text:
            return None
        parts = text.split(",")
        if len(parts) != 4:
            return None
        try:
            return tuple(int(part) for part in parts)
        except ValueError:
            return None

    for line in lines:
        if not line.startswith("wine_window "):
            continue
        quoted = {
            match.group(1): match.group(2)
            for match in re.finditer(r'(\w+)="((?:\\.|[^"])*)"', line)
        }
        unquoted = {
            match.group(1): match.group(2)
            for match in re.finditer(r"(\w+)=([^\s\"]+)", line)
            if match.group(1) not in quoted
        }
        fields = {**unquoted, **quoted}
        rect = parse_rect_text(fields.get("rect"))
        client = parse_rect_text(fields.get("client"))
        monitor = parse_rect_text(fields.get("monitor"))
        work = parse_rect_text(fields.get("work"))
        if not rect or not client or not monitor:
            continue
        item = {
            "hwnd": fields.get("hwnd", ""),
            "visible": bool(int(fields.get("visible", "0"))),
            "pid": int(fields.get("pid", "0")),
            "tid": int(fields.get("tid", "0")),
            "style": int(fields.get("style", "0"), 16),
            "exstyle": int(fields.get("exstyle", "0"), 16),
            "rect": rect,
            "client": client,
            "monitor": monitor,
            "work": work,
            "monitor_name": fields.get("monitor_name", ""),
            "class": fields.get("class", ""),
            "title": fields.get("title", ""),
            "exe": fields.get("exe", ""),
        }
        haystack = f"{item['title']} {item['class']} {item['exe']}".lower()
        item["sc_candidate"] = any(
            token in haystack
            for token in ("starcitizen", "star citizen", "cryengine", "rsi launcher", "roberts space")
        )
        windows.append(item)
    return {
        "windows": windows,
        "visible": [item for item in windows if item["visible"]],
        "candidates": sorted(
            [item for item in windows if item["sc_candidate"]],
            key=lambda item: (not item["visible"], -rect_area(item["rect"])),
        ),
    }


def parse_sc_context(prefix: Path) -> dict:
    live_dir = prefix / "drive_c/Program Files/Roberts Space Industries/StarCitizen/LIVE"
    attrs_path = live_dir / "user/client/0/Profiles/default/attributes.xml"
    launch_script = prefix / "sc-launch.sh"
    attrs = read_attrs(attrs_path)
    launch_text = "\n".join(read_lines(launch_script))
    logs = [
        prefix / "sc-prime.log",
        prefix / "sc-launch.log",
        live_dir / "Game.log",
    ]
    dll_loaded = False
    npclient_loaded = False
    api_trace = Counter()
    interesting_lines = []
    for log in logs:
        for line in read_lines(log):
            lower = line.lower()
            if "tobii_gameintegration_x64.dll" in lower and ("loaded" in lower or "loaddll" in lower):
                dll_loaded = True
                if len(interesting_lines) < 4:
                    interesting_lines.append(f"{log.name}: {line.strip()}")
            if "npclient64.dll" in lower and ("loaded" in lower or "loaddll" in lower):
                npclient_loaded = True
            if "snoop" in lower or "call " in lower or "ret " in lower:
                for symbol in API_TRACE_SYMBOLS:
                    if symbol.lower() in lower:
                        api_trace[symbol] += 1
    return {
        "attrs_path": attrs_path,
        "attrs": attrs,
        "launch_script": launch_script,
        "native_launch_hook": "tobii-linux-native-tobii-hook begin" in launch_text,
        "api_trace_hook": "tobii-linux-tobii-api-trace-hook begin" in launch_text,
        "launch_kills_wineserver": "wineserver -k" in launch_text,
        "dll_loaded": dll_loaded,
        "npclient_loaded": npclient_loaded,
        "api_trace": api_trace,
        "interesting_lines": interesting_lines,
    }


def status_line(ok: bool, label: str, detail: str) -> str:
    return f"[{'ok' if ok else '!!'}] {label}: {detail}"


def fmt_obj(obj: int) -> str:
    return f"0x{obj:04x}/{OBJECT_NAMES.get(obj, 'unknown')}"


def fmt_stream(stream: int) -> str:
    return f"0x{stream:04x}/{STREAM_NAMES.get(stream, 'unknown')}"


def fmt_sesp_types(types: Counter) -> str:
    names = {
        2: "initialize",
        4: "status",
        9: "display_info",
        11: "list_devices",
        26: "feature_update",
        50: "session_metadata",
    }
    if not types:
        return "none"
    return ", ".join(f"{t}/{names.get(t, 'unknown')} x{n}" for t, n in sorted(types.items()))


def fmt_rect(rect: tuple[int, int, int, int] | None) -> str:
    if not rect:
        return "none"
    return ",".join(str(value) for value in rect)


def rect_area(rect: tuple[int, int, int, int] | None) -> int:
    if not rect:
        return 0
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def rect_overlaps(a: tuple[int, int, int, int] | None, b: tuple[int, int, int, int] | None) -> bool:
    if not a or not b:
        return False
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def classify_natural_table(kick: dict) -> tuple[bool, str]:
    if not kick.get("exists"):
        return False, "no natural harness log"

    entries = [
        item for item in kick.get("connector_entries", [])
        if item.get("phase") in {"after_discovery_wait", "before_TrackWindow", "after_TrackWindow"}
    ]
    if not entries:
        gates = [g for g in kick.get("connector_gates", []) if g.get("phase") in {"after_discovery_wait", "before_TrackWindow", "after_TrackWindow"}]
        if gates:
            counts = ",".join(f"{g['phase']}:{g['count']}" for g in gates)
            return False, f"no entries dumped; table counts {counts}"
        return False, "no connector table dump"

    selector_rect = kick.get("selector_rect")
    by_phase = defaultdict(list)
    for entry in entries:
        by_phase[entry["phase"]].append(entry)
    phase_parts = []
    valid_candidates = []
    reasons = Counter()
    for phase in ("after_discovery_wait", "before_TrackWindow", "after_TrackWindow"):
        phase_entries = by_phase.get(phase, [])
        if not phase_entries:
            continue
        active = sum(1 for e in phase_entries if e["flag_active"])
        nonzero_rect = sum(1 for e in phase_entries if rect_area(e["rect"]) > 0)
        overlap = sum(1 for e in phase_entries if rect_overlaps(e["rect"], selector_rect))
        phase_parts.append(f"{phase}:entries={len(phase_entries)} active={active} rect={nonzero_rect} overlap={overlap}")
        for entry in phase_entries:
            if not entry["flag_active"]:
                reasons["inactive"] += 1
                continue
            if rect_area(entry["rect"]) <= 0:
                reasons["zero_rect"] += 1
                continue
            if selector_rect and not rect_overlaps(entry["rect"], selector_rect):
                reasons["non_overlapping_rect"] += 1
                continue
            if entry["type"] not in (0, 5):
                reasons["wrong_type"] += 1
                continue
            if entry["caps"] == 0:
                reasons["missing_caps"] += 1
                continue
            valid_candidates.append(entry)

    if valid_candidates:
        best = valid_candidates[-1]
        return True, (
            f"valid candidate phase={best['phase']} index={best['index']} "
            f"rect={fmt_rect(best['rect'])} type={best['type']} caps={fmt_hex_or_none(best['caps'])} "
            f"url={best['url'] or 'empty'}; " + "; ".join(phase_parts)
        )
    reason_detail = ", ".join(f"{k}={v}" for k, v in reasons.items()) or "no candidates"
    return False, f"{reason_detail}; selector_rect={fmt_rect(selector_rect)}; " + "; ".join(phase_parts)


def fmt_hex_or_none(value: int | None, width: int = 8) -> str:
    if value is None:
        return "none"
    return f"0x{value:0{width}x}"


def fmt_dt(base: float | None, ts: float | None) -> str:
    if base is None or ts is None:
        return "n/a"
    delta = ts - base
    return f"{delta:+.3f}s"


def tcp_session_summary(session: dict) -> str:
    objects = session.get("objects") or []
    subscriptions = session.get("subscriptions") or []
    start = session.get("start")
    end = session.get("end")
    duration = "open" if start is None or end is None else f"{end - start:.3f}s"
    phases = []
    if objects:
        phases.append(f"last={fmt_obj(objects[-1])}")
    if 0x0672 in objects and 0x0C62 in objects:
        phases.append("runtime=ok")
    if subscriptions:
        phases.append("subs=" + "/".join(fmt_stream(s) for s in subscriptions))
    if session.get("stream_events"):
        phases.append("presence_events=" + str(sum(session["stream_events"].values())))
    if session.get("gaze_events"):
        phases.append(f"gaze_events={session['gaze_events']}")
    return (
        f"origin={session.get('origin', 'unknown')} duration={duration} rx={session.get('rx')} tx={session.get('tx')} "
        f"objects={len(objects)} {' '.join(phases)}"
    )


def first_time_for_obj(session: dict, obj: int) -> float | None:
    for seen_obj, ts in zip(session.get("objects", []), session.get("object_times", [])):
        if seen_obj == obj:
            return ts
    return None


def first_time_for_subscription(session: dict, stream: int) -> float | None:
    for seen_stream, ts in zip(session.get("subscriptions", []), session.get("subscription_times", [])):
        if seen_stream == stream:
            return ts
    return None


def print_adoption_timeline(tcp: dict, pipe: dict) -> None:
    richest = tcp.get("richest")
    pipe_times = pipe["first_times"]
    print()
    print("adoption_timeline:")
    if richest:
        tcp_candidates = [
            ("hello", first_time_for_obj(richest, 0x03E8)),
            ("discovery complete/session metadata", first_time_for_obj(richest, 0x083E)),
            ("runtime metadata 0x0672", first_time_for_obj(richest, 0x0672)),
            ("runtime metadata 0x0c62", first_time_for_obj(richest, 0x0C62)),
            ("presence subscribe", first_time_for_subscription(richest, 0x0504)),
            ("gaze subscribe", first_time_for_subscription(richest, 0x0500)),
            ("presence event", richest.get("first_stream_event")),
            ("gaze event", richest.get("first_gaze_event")),
            ("tcp close", richest.get("end")),
        ]
        tcp_seen = [(label, ts) for label, ts in tcp_candidates if ts is not None]
        if tcp_seen:
            base = richest.get("start") or tcp_seen[0][1]
            print("  tcp richest session:")
            for label, ts in tcp_seen:
                print(f"    {fmt_dt(base, ts)} {label}")

    pipe_candidates = [
        ("pipe attach", pipe_times.get("pipe_accept")),
        ("initialize", pipe_times.get(("sesp_type", 2))),
        ("display info", pipe_times.get(("sesp_type", 9))),
        ("session metadata/status 0x32", pipe_times.get(("sesp_type", 50))),
        ("feature update 0x1a", pipe_times.get(("sesp_type", 26))),
        ("linux live pose udp", pipe_times.get("live_pose_udp_packet")),
        ("headpose event", pipe_times.get("sesp_synthetic_headpose")),
    ]
    pipe_seen = [(label, ts) for label, ts in pipe_candidates if ts is not None]
    if pipe_seen:
        base = pipe_times.get("pipe_accept") or min(ts for _label, ts in pipe_seen if ts is not None)
        print("  sesp pipe/log clock:")
        for label, ts in pipe_seen:
            print(f"    {fmt_dt(base, ts)} {label}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Star Citizen Tobii runtime logs.")
    parser.add_argument(
        "--dir",
        default=".tmp/sc-tobii-native-runtime",
        help="runtime directory containing middleware-spy.log and middleware-pipe-spy.log",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=3,
        help="number of richest TCP sessions to summarize",
    )
    parser.add_argument(
        "--star-citizen-prefix",
        default=str(Path.home() / "Games/star-citizen"),
        help="Star Citizen Wine prefix/root used to summarize profile and launcher logs",
    )
    args = parser.parse_args()

    runtime_dir = Path(args.dir)
    tcp_log = runtime_dir / "middleware-spy.log"
    pipe_log = runtime_dir / "middleware-pipe-spy.log"
    etdefault_log = runtime_dir / "etdefaultpipe-spy.log"
    tobii_prefixed_log = runtime_dir / "tobii-prefixed-pipe-spy.log"
    tobiiprp_prefixed_log = runtime_dir / "tobiiprp-prefixed-pipe-spy.log"
    wine_window_probe_log = runtime_dir / "wine-window-probe.log"
    adoption_kick_log = runtime_dir / "adoption-kick.log"
    trackwindow_kick_log = runtime_dir / "trackwindow-kick.log"
    natural_kick_log = runtime_dir / "natural-table-kick.log"
    tcp = parse_tcp(read_lines(tcp_log))
    pipe = parse_pipe(read_lines(pipe_log))
    etdefault_lines = read_lines(etdefault_log)
    etdefault_listening = sum(1 for line in etdefault_lines if "mode=etdefaultpipe" in line or "ETDefaultPIPE" in line)
    etdefault_requests = sum(1 for line in etdefault_lines if "etdefaultpipe_request" in line)
    etdefault_responses = sum(1 for line in etdefault_lines if "etdefaultpipe_send" in line)
    tobii_prefixed_lines = read_lines(tobii_prefixed_log)
    tobiiprp_prefixed_lines = read_lines(tobiiprp_prefixed_log)
    wine_windows = parse_wine_window_probe(read_lines(wine_window_probe_log))
    tobii_prefixed_listening = sum(1 for line in tobii_prefixed_lines if "TOBII-" in line or "pipe_listening" in line)
    tobii_prefixed_accepts = sum(1 for line in tobii_prefixed_lines if "pipe_accept" in line)
    tobiiprp_prefixed_listening = sum(1 for line in tobiiprp_prefixed_lines if "TOBIIPRP-" in line or "pipe_listening" in line)
    tobiiprp_prefixed_accepts = sum(1 for line in tobiiprp_prefixed_lines if "pipe_accept" in line)
    kick = parse_kick_log(adoption_kick_log)
    trackwindow_kick = parse_kick_log(trackwindow_kick_log)
    natural_kick = parse_kick_log(natural_kick_log)
    sc_context = parse_sc_context(Path(args.star_citizen_prefix))
    harness_origins = {"adoption-kick", "trackwindow-kick", "natural-table-kick"}
    sc_tcp_sessions = [s for s in tcp["non_empty_sessions"] if s.get("origin") not in harness_origins]
    kick_tcp_sessions = [s for s in tcp["non_empty_sessions"] if s.get("origin") == "adoption-kick"]
    trackwindow_tcp_sessions = [s for s in tcp["non_empty_sessions"] if s.get("origin") == "trackwindow-kick"]
    natural_tcp_sessions = [s for s in tcp["non_empty_sessions"] if s.get("origin") == "natural-table-kick"]
    sc_tcp = summarize_sessions(sc_tcp_sessions)
    kick_tcp = summarize_sessions(kick_tcp_sessions)
    trackwindow_tcp = summarize_sessions(trackwindow_tcp_sessions)
    natural_tcp = summarize_sessions(natural_tcp_sessions)

    print(f"runtime_dir={runtime_dir}")
    print(f"tcp_log={tcp_log} exists={tcp_log.exists()}")
    print(f"pipe_log={pipe_log} exists={pipe_log.exists()}")
    print(f"etdefaultpipe_log={etdefault_log} exists={etdefault_log.exists()}")
    print(f"tobii_prefixed_pipe_log={tobii_prefixed_log} exists={tobii_prefixed_log.exists()}")
    print(f"tobiiprp_prefixed_pipe_log={tobiiprp_prefixed_log} exists={tobiiprp_prefixed_log.exists()}")
    print(f"wine_window_probe_log={wine_window_probe_log} exists={wine_window_probe_log.exists()}")
    print()

    richest = tcp["richest"]
    print(status_line(bool(tcp["non_empty_sessions"]), "tcp probe", f"{len(tcp['non_empty_sessions'])} non-empty session(s), {tcp['empty_connects']} empty reconnect(s)"))
    if tcp["kick_intervals"]:
        interval_detail = ", ".join(
            f"{i.get('name')} pid={i.get('pid')} start={i.get('start'):.3f} end={i.get('end'):.3f} rc={i.get('rc')}"
            for i in tcp["kick_intervals"]
            if i.get("start") is not None and i.get("end") is not None
        ) or f"{len(tcp['kick_intervals'])} active/partial marker(s)"
        print(status_line(True, "kick markers", interval_detail))
    if tcp["adoption_intervals"]:
        interval_detail = ", ".join(
            f"pid={i.get('pid')} start={i.get('start'):.3f} end={i.get('end'):.3f} rc={i.get('rc')}"
            for i in tcp["adoption_intervals"]
            if i.get("start") is not None and i.get("end") is not None
        ) or f"{len(tcp['adoption_intervals'])} active/partial marker(s)"
        print(status_line(True, "adoption-kick markers", interval_detail))
    elif kick["exists"]:
        print(status_line(False, "adoption-kick markers", "missing; rerun make sc-tobii-stock-adoption-kick for SC-vs-harness attribution"))
    if richest:
        sequence = " -> ".join(fmt_obj(obj) for obj in richest["objects"])
        print(f"richest_sequence={sequence}")
        print(f"richest_rx={richest.get('rx')} richest_tx={richest.get('tx')}")
        ranked_sessions = sorted(
            tcp["non_empty_sessions"],
            key=lambda s: (len(s.get("objects", [])), len(s.get("subscriptions", [])), s.get("gaze_events", 0)),
            reverse=True,
        )
        for idx, session in enumerate(ranked_sessions[: max(args.sessions, 0)], start=1):
            print(f"tcp_session[{idx}] {tcp_session_summary(session)}")
    if sc_tcp_sessions and (kick_tcp_sessions or trackwindow_tcp_sessions or natural_tcp_sessions):
        sc_richest = sc_tcp["richest"]
        kick_richest = kick_tcp["richest"]
        trackwindow_richest = trackwindow_tcp["richest"]
        natural_richest = natural_tcp["richest"]
        sc_last = fmt_obj(sc_richest["objects"][-1]) if sc_richest and sc_richest.get("objects") else "none"
        kick_last = fmt_obj(kick_richest["objects"][-1]) if kick_richest and kick_richest.get("objects") else "none"
        trackwindow_last = fmt_obj(trackwindow_richest["objects"][-1]) if trackwindow_richest and trackwindow_richest.get("objects") else "none"
        natural_last = fmt_obj(natural_richest["objects"][-1]) if natural_richest and natural_richest.get("objects") else "none"
        print(f"sc_tcp_summary sessions={len(sc_tcp_sessions)} runtime={int(all(sc_tcp['objects'].get(obj, 0) for obj in (0x0672, 0x0C62)))} subs={sum(sc_tcp['subscriptions'].values())} last={sc_last}")
        if kick_tcp_sessions:
            print(f"adoption_kick_tcp_summary sessions={len(kick_tcp_sessions)} runtime={int(all(kick_tcp['objects'].get(obj, 0) for obj in (0x0672, 0x0C62)))} subs={sum(kick_tcp['subscriptions'].values())} last={kick_last}")
        if trackwindow_tcp_sessions:
            print(f"trackwindow_kick_tcp_summary sessions={len(trackwindow_tcp_sessions)} runtime={int(all(trackwindow_tcp['objects'].get(obj, 0) for obj in (0x0672, 0x0C62)))} subs={sum(trackwindow_tcp['subscriptions'].values())} last={trackwindow_last}")
        if natural_tcp_sessions:
            print(f"natural_table_tcp_summary sessions={len(natural_tcp_sessions)} runtime={int(all(natural_tcp['objects'].get(obj, 0) for obj in (0x0672, 0x0C62)))} subs={sum(natural_tcp['subscriptions'].values())} last={natural_last}")

    runtime_seen = all(tcp["objects"].get(obj, 0) for obj in (0x0672, 0x0C62))
    subscribe_seen = bool(tcp["subscriptions"])
    sc_runtime_seen = all(sc_tcp["objects"].get(obj, 0) for obj in (0x0672, 0x0C62))
    sc_subscribe_seen = bool(sc_tcp["subscriptions"])
    kick_runtime_seen = all(kick_tcp["objects"].get(obj, 0) for obj in (0x0672, 0x0C62))
    kick_subscribe_seen = bool(kick_tcp["subscriptions"])
    trackwindow_runtime_seen = all(trackwindow_tcp["objects"].get(obj, 0) for obj in (0x0672, 0x0C62))
    trackwindow_subscribe_seen = bool(trackwindow_tcp["subscriptions"])
    natural_runtime_seen = all(natural_tcp["objects"].get(obj, 0) for obj in (0x0672, 0x0C62))
    natural_subscribe_seen = bool(natural_tcp["subscriptions"])
    print(status_line(runtime_seen, "runtime metadata", "saw 0x0672 and 0x0c62" if runtime_seen else "not reached"))
    if kick_tcp_sessions or trackwindow_tcp_sessions or natural_tcp_sessions:
        print(status_line(sc_runtime_seen, "SC/unknown runtime metadata", "saw 0x0672 and 0x0c62" if sc_runtime_seen else "not reached outside harness kicks"))
    if kick_tcp_sessions:
        print(status_line(kick_runtime_seen, "adoption-kick runtime metadata", "saw 0x0672 and 0x0c62" if kick_runtime_seen else "not reached"))
    if trackwindow_tcp_sessions:
        print(status_line(trackwindow_runtime_seen, "trackwindow-kick runtime metadata", "saw 0x0672 and 0x0c62" if trackwindow_runtime_seen else "not reached"))
    if natural_tcp_sessions:
        print(status_line(natural_runtime_seen, "natural-table-kick runtime metadata", "saw 0x0672 and 0x0c62" if natural_runtime_seen else "not reached"))
    print(status_line(subscribe_seen, "stream subscriptions", ", ".join(f"{fmt_stream(s)} x{n}" for s, n in tcp["subscriptions"].items()) or "none"))
    if kick_tcp_sessions or trackwindow_tcp_sessions:
        sc_subs = ", ".join(f"{fmt_stream(s)} x{n}" for s, n in sc_tcp["subscriptions"].items()) or "none"
        print(status_line(sc_subscribe_seen, "SC/unknown stream subscriptions", sc_subs))
    if kick_tcp_sessions:
        kick_subs = ", ".join(f"{fmt_stream(s)} x{n}" for s, n in kick_tcp["subscriptions"].items()) or "none"
        print(status_line(kick_subscribe_seen, "adoption-kick stream subscriptions", kick_subs))
    if trackwindow_tcp_sessions:
        trackwindow_subs = ", ".join(f"{fmt_stream(s)} x{n}" for s, n in trackwindow_tcp["subscriptions"].items()) or "none"
        print(status_line(trackwindow_subscribe_seen, "trackwindow-kick stream subscriptions", trackwindow_subs))
    if natural_tcp_sessions:
        natural_subs = ", ".join(f"{fmt_stream(s)} x{n}" for s, n in natural_tcp["subscriptions"].items()) or "none"
        print(status_line(natural_subscribe_seen, "natural-table-kick stream subscriptions", natural_subs))
    print(status_line(bool(tcp["stream_events"] or tcp["gaze_events"]), "async stream events", f"presence={sum(tcp['stream_events'].values())} gaze={tcp['gaze_events']}"))
    print(status_line(tcp["live_gaze_udp"] > 0, "linux gaze producer", f"live_gaze_udp={tcp['live_gaze_udp']} valid={tcp['live_gaze_valid']}"))
    live_gaze_delivery_ok = tcp["live_gaze_events"] > 0 and tcp["live_gaze_event_valid"] > 0 and tcp["live_gaze_udp"] > 0
    live_gaze_detail = (
        f"live_gaze_events={tcp['live_gaze_events']} "
        f"valid_events={tcp['live_gaze_event_valid']} "
        f"producer_packets={tcp['live_gaze_udp']} producer_valid={tcp['live_gaze_valid']}"
    )
    print(status_line(live_gaze_delivery_ok, "stock-dll live gaze delivery", live_gaze_detail))
    if kick["exists"]:
        supported = ",".join(f"{mask:#x}:{value}" for mask, value in sorted(kick["stream_supported"].items())) or "none"
        kick_detail = (
            f"present={kick['present']} enabled={kick['enabled']} initial_connected={kick['connected_initial']} "
            f"TrackTracker={'yes' if kick['tracktracker_called'] else 'no'} rc={kick['tracktracker_rc']} "
            f"connected={'yes' if kick['connected'] else 'no'} "
            f"updates={kick['updates']} headpose_ok={kick['headpose_ok']} gaze_ok={kick['gaze_ok']} "
            f"streams={supported}"
        )
        print(status_line(kick["tracktracker_ok"] and kick["connected"] and kick["headpose_ok"] > 0 and kick["gaze_ok"] > 0, "adoption-kick harness proof", kick_detail))
    if trackwindow_kick["exists"]:
        supported = ",".join(f"{mask:#x}:{value}" for mask, value in sorted(trackwindow_kick["stream_supported"].items())) or "none"
        trackwindow_detail = (
            f"present={trackwindow_kick['present']} enabled={trackwindow_kick['enabled']} initial_connected={trackwindow_kick['connected_initial']} "
            f"TrackWindow={'yes' if trackwindow_kick['trackwindow_called'] else 'no'} rc={trackwindow_kick['trackwindow_rc']} "
            f"connected={'yes' if trackwindow_kick['connected'] else 'no'} "
            f"updates={trackwindow_kick['updates']} headpose_ok={trackwindow_kick['headpose_ok']} gaze_ok={trackwindow_kick['gaze_ok']} "
            f"streams={supported}"
        )
        print(status_line(trackwindow_kick["trackwindow_ok"] and trackwindow_runtime_seen and trackwindow_subscribe_seen, "trackwindow-kick harness proof", trackwindow_detail))
        if trackwindow_kick["seeded_device_table"]:
            seed = trackwindow_kick["seed"]
            seed_detail = (
                f"active_bank={seed['active_bank']} flags={seed['flags']} prev_flags={seed['previous_flags']} "
                f"rect={fmt_rect(seed['rect'])} status={fmt_hex_or_none(seed['status'])} "
                f"type={seed['type']} caps={fmt_hex_or_none(seed['caps'])}"
            )
            print(status_line(True, "trackwindow seed table", seed_detail))
        if trackwindow_kick["selector_index"] is not None:
            selector_index = trackwindow_kick["selector_index"]
            selected = selector_index != 0xFFFFFFFF
            window_selector_detail = "; ".join(
                f"{item['phase']} {'destructive' if item.get('destructive') else 'passive'} "
                f"count={item['count']} bank={item['bank']} flags={item['flags']} "
                f"rect={fmt_rect(item['rect'])} selector={fmt_hex_or_none(item['selector_index'])} "
                f"manual={fmt_hex_or_none(item.get('manual_selector_index'))}"
                for item in trackwindow_kick.get("window_selectors", [])
            )
            selector_detail = (
                f"mode={'probe-only' if trackwindow_kick.get('selector_probe_only') else 'force-select'} "
                f"selector={fmt_hex_or_none(selector_index)} forced_index={trackwindow_kick['selector_forced_index']} "
                f"rect={fmt_rect(trackwindow_kick['selector_rect'])} mask={fmt_hex_or_none(trackwindow_kick['selector_mask'])} "
                f"provider_select_rc={trackwindow_kick['provider_select_rc']} "
                f"provider_subscribe_rc={trackwindow_kick['provider_subscribe_rc']}"
            )
            passive_manual_selected = any(
                not item.get("destructive")
                and item.get("manual_selector_index") not in (None, 0xFFFFFFFF)
                for item in trackwindow_kick.get("window_selectors", [])
            )
            print(status_line(selected or passive_manual_selected, "trackwindow selector checkpoint", selector_detail))
            if window_selector_detail:
                pre_selected = any(
                    item["phase"] == "before_TrackWindow"
                    and (
                        item["selector_index"] != 0xFFFFFFFF
                        or item.get("manual_selector_index") not in (None, 0xFFFFFFFF)
                    )
                    for item in trackwindow_kick.get("window_selectors", [])
                )
                print(status_line(pre_selected, "trackwindow selector probes", window_selector_detail))
        if trackwindow_kick["forced_provider_connected"]:
            state = trackwindow_kick["forced_provider_state"]
            force_detail = (
                f"bytes={state['byte20']},{state['byte21']},{state['byte22']} state={state['state']} "
                f"status_a={fmt_hex_or_none(state['status_a'])} status_b={fmt_hex_or_none(state['status_b'])}"
            )
            print(status_line(False, "forced provider mutation", force_detail))
    if natural_kick["exists"]:
        natural_supported = ",".join(f"{mask:#x}:{value}" for mask, value in sorted(natural_kick["stream_supported"].items())) or "none"
        natural_detail = (
            f"present={natural_kick['present']} enabled={natural_kick['enabled']} initial_connected={natural_kick['connected_initial']} "
            f"TrackWindow={'yes' if natural_kick['trackwindow_called'] else 'no'} rc={natural_kick['trackwindow_rc']} "
            f"connected={'yes' if natural_kick['connected'] else 'no'} "
            f"updates={natural_kick['updates']} headpose_ok={natural_kick['headpose_ok']} gaze_ok={natural_kick['gaze_ok']} "
            f"streams={natural_supported}"
        )
        print(status_line(natural_kick["trackwindow_ok"] and natural_runtime_seen and natural_subscribe_seen, "natural TrackWindow harness proof", natural_detail))
        if natural_kick.get("track_window_identities"):
            identity_detail = "; ".join(
                f"{item['phase']} rect={fmt_rect(item['rect'])} client={fmt_rect(item['client'])} "
                f"monitor={fmt_rect(item['monitor'])} class={item['class']} title={item['title']}"
                for item in natural_kick["track_window_identities"][-3:]
            )
            print(status_line(True, "natural harness window identity", identity_detail))
        table_ok, table_detail = classify_natural_table(natural_kick)
        print(status_line(table_ok, "natural discovery table", table_detail))

    if wine_window_probe_log.exists():
        candidate_detail = "; ".join(
            f"hwnd={item['hwnd']} rect={fmt_rect(item['rect'])} client={fmt_rect(item['client'])} "
            f"monitor={fmt_rect(item['monitor'])} class={item['class']} title={item['title']}"
            for item in wine_windows["candidates"][:6]
        ) or f"no SC candidates; visible={len(wine_windows['visible'])} total={len(wine_windows['windows'])}"
        print(status_line(bool(wine_windows["candidates"]), "Wine SC window candidates", candidate_detail))

    pipe_counts = pipe["counts"]
    pipe_types = pipe["request_types"]
    pipe_accept = pipe_counts["pipe_accept"]
    live_udp = pipe_counts["live_pose_udp_packet"]
    headpose = pipe_counts["sesp_synthetic_headpose"]
    provider_nudge_attempt = pipe_counts["sesp_provider_nudge_attempt"]
    provider_nudge_ok = pipe_counts["sesp_provider_nudge_ok"]
    provider_nudge_failed = pipe_counts["sesp_provider_nudge_failed"]
    display_info_seen = pipe_types.get(9, 0) > 0
    provider_latch_seen = pipe_types.get(50, 0) > 0 or pipe_types.get(26, 0) > 0
    print(status_line(pipe_accept > 0, "sesp pipe attach", f"pipe_accept={pipe_accept}"))
    print(status_line(display_info_seen, "sesp display info", f"request_types={fmt_sesp_types(pipe_types)}"))
    print(status_line(provider_latch_seen, "sesp provider latch", "saw 0x32/0x1a requests" if provider_latch_seen else "missing 0x32/0x1a requests"))
    if provider_nudge_attempt or provider_nudge_ok or provider_nudge_failed:
        nudge_detail = f"attempts={provider_nudge_attempt} written={provider_nudge_ok} failed={provider_nudge_failed}"
    else:
        nudge_detail = "none"
    print(status_line(provider_nudge_ok > 0, "sesp provider nudge", nudge_detail))
    print(status_line(live_udp > 0, "linux pose producer", f"live_pose_udp_packet={live_udp} sources={dict(pipe['live_sources'])}"))
    print(status_line(headpose > 0, "stock-dll headpose delivery", f"sesp_synthetic_headpose={headpose}"))
    if etdefault_log.exists():
        print(status_line(etdefault_requests > 0, "ETDefaultPIPE discovery", f"listening={etdefault_listening} requests={etdefault_requests} responses={etdefault_responses}"))
    if tobii_prefixed_log.exists():
        print(status_line(tobii_prefixed_listening > 0, "TOBII-* discovery marker", f"listening={tobii_prefixed_listening} accepts={tobii_prefixed_accepts}"))
    if tobiiprp_prefixed_log.exists():
        print(status_line(tobiiprp_prefixed_listening > 0, "TOBIIPRP-* discovery marker", f"listening={tobiiprp_prefixed_listening} accepts={tobiiprp_prefixed_accepts}"))

    attrs = sc_context["attrs"]
    source_value = attrs.get("HeadtrackingSource")
    source_name = HEADTRACKING_SOURCES.get(source_value or "", "unknown")
    toggle_value = attrs.get("HeadtrackingToggle", "missing")
    print(status_line(source_value == "3", "SC headtracking source", f"value={source_value or 'missing'} name={source_name} toggle={toggle_value}"))
    print(status_line(sc_context["dll_loaded"], "SC stock Tobii DLL load", "loaded" if sc_context["dll_loaded"] else "not found in launcher/game logs"))
    print(status_line(sc_context["npclient_loaded"], "SC TrackIR DLL load", "loaded" if sc_context["npclient_loaded"] else "not found in launcher/game logs"))
    hook_detail = (
        f"native={'yes' if sc_context['native_launch_hook'] else 'no'} "
        f"api-trace={'yes' if sc_context['api_trace_hook'] else 'no'} "
        f"wineserver-kill={'yes' if sc_context['launch_kills_wineserver'] else 'no'}"
    )
    print(status_line(sc_context["native_launch_hook"], "SC launch hook", hook_detail))
    trace_counts = sc_context["api_trace"]
    trace_detail = ", ".join(f"{name}={count}" for name, count in trace_counts.items()) or "none"
    print(status_line(bool(trace_counts), "Wine Tobii API trace", trace_detail))
    print_adoption_timeline(tcp, pipe)

    print()
    trackwindow_has_destructive_selector = any(
        item.get("destructive") for item in trackwindow_kick.get("window_selectors", [])
    )
    trackwindow_has_passive_manual_candidate = any(
        not item.get("destructive")
        and item.get("manual_selector_index") not in (None, 0xFFFFFFFF)
        for item in trackwindow_kick.get("window_selectors", [])
    )
    natural_table_ok, natural_table_detail = classify_natural_table(natural_kick)
    if sc_runtime_seen and sc_subscribe_seen:
        print("diagnosis=Star Citizen's own stock Tobii DLL session naturally reached runtime metadata and subscribed to streams.")
        if live_udp <= 0 or tcp["live_gaze_udp"] <= 0:
            missing = []
            if live_udp <= 0:
                missing.append("pose UDP 127.0.0.1:4243")
            if tcp["live_gaze_udp"] <= 0:
                missing.append("gaze UDP 127.0.0.1:4457")
            print(f"next=keep the stock runtime path; start or fix the dashboard producer for {', '.join(missing)} before in-game validation.")
        elif tcp["live_gaze_event_valid"] <= 0:
            print("next=stock DLL is subscribed, but gaze events are stale/invalid; verify dashboard gaze calibration/valid gaze and the 127.0.0.1:4457 sender.")
        else:
            print("next=test in-game with the stock DLL: head pose and gaze targeting should now be driven by live dashboard data.")
    elif natural_kick["exists"] and natural_runtime_seen and natural_subscribe_seen and not sc_runtime_seen:
        print("diagnosis=Natural TrackWindow adoption works in the harness, but Star Citizen's own stock DLL session still stops before runtime metadata.")
        print("next=compare SC window/display identity against the harness window; the protocol path is good, but SC is selecting a different or unbound surface.")
    elif natural_kick["exists"] and not natural_table_ok:
        print(f"diagnosis=Natural discovery did not produce a selectable provider entry: {natural_table_detail}")
        print("next=compare TCP 0x0596/session metadata and SESP display-info/list-device fields against the Windows oracle, then adjust display binding only.")
    elif natural_kick["exists"] and natural_table_ok and not natural_runtime_seen:
        print("diagnosis=Natural discovery has a selectable-looking provider entry, but TrackWindow still does not advance to runtime metadata/subscriptions.")
        print("next=inspect provider rank/type/caps/status fields and the exact selector branch around the valid-looking entry.")
    elif trackwindow_tcp_sessions and trackwindow_runtime_seen and trackwindow_subscribe_seen and not sc_runtime_seen:
        print("diagnosis=TrackWindow with a real harness window can naturally adopt the stock DLL runtime, but Star Citizen's own stock DLL session still stops before runtime metadata.")
        print("next=focus on why SC's in-process TrackWindow/display binding differs from the harness window: HWND/monitor/display metadata, settings gate, or API call ordering.")
    elif (
        trackwindow_kick["exists"]
        and trackwindow_kick["trackwindow_called"]
        and trackwindow_kick["selector_index"] == 0xFFFFFFFF
        and not trackwindow_runtime_seen
        and trackwindow_has_destructive_selector
    ):
        print("diagnosis=TrackWindow reached the window-to-tracker selector, but the selector returned no tracker for the window rectangle.")
        print("next=focus only on the stream-provider discovered-device table: active-bank flags, entry count, tracker type/rank, rectangle overlap, and display binding.")
    elif (
        trackwindow_kick["exists"]
        and trackwindow_kick["trackwindow_called"]
        and trackwindow_has_passive_manual_candidate
        and not trackwindow_runtime_seen
    ):
        print("diagnosis=The passive stream-provider table says TrackWindow should have a candidate, but TrackWindow still did not advance to runtime metadata/subscriptions.")
        print("next=instrument the non-selector connector gates: enabled byte, current index, provider status object, and provider-select return code without calling the destructive selector probe.")
    elif (
        trackwindow_kick["exists"]
        and trackwindow_kick["trackwindow_called"]
        and trackwindow_kick["selector_index"] is not None
        and trackwindow_kick["selector_index"] != 0xFFFFFFFF
        and not trackwindow_runtime_seen
    ):
        print("diagnosis=TrackWindow selected a tracker index, but that selection did not advance to runtime metadata/subscriptions.")
        print("next=compare the downstream provider-select/connect path against the working TrackTracker path; do not keep changing discovery until provider select rc and state transitions are understood.")
    elif trackwindow_kick["forced_provider_connected"] and not trackwindow_runtime_seen:
        print("diagnosis=Forced provider-connected bytes were written, but the stock DLL still did not enter runtime metadata/subscriptions.")
        print("next=park forced-state mutation; the missing transition is an earlier real API call or callback, not just final provider status bytes.")
    elif trackwindow_kick["exists"] and trackwindow_kick["trackwindow_called"] and not trackwindow_runtime_seen:
        print("diagnosis=TrackWindow was called successfully in the harness, but it still did not trigger runtime metadata/subscriptions.")
        print("next=the remaining natural-adoption gate is likely display/provider discovery data, not SESP or live gaze/headpose delivery.")
    elif kick["exists"] and not tcp["adoption_intervals"] and runtime_seen and subscribe_seen:
        print("diagnosis=The stock DLL harness proof succeeded, and the log contains a fully adopted TCP session, but this run is unmarked so it cannot be attributed to Star Citizen vs the adoption-kick harness.")
        print("next=rerun make sc-tobii-stock-adoption-kick with the updated marker support, then rerun make sc-tobii-stock-runtime-status to separate SC sessions from harness sessions.")
    elif kick_tcp_sessions and kick_runtime_seen and kick_subscribe_seen and not sc_runtime_seen:
        print("diagnosis=The stock DLL harness adopts successfully and receives live headpose/gaze, but Star Citizen's own stock DLL session still stops before runtime metadata.")
        print("next=the middleware/SESP protocol is good; focus on the in-process API condition that makes SC call TrackTracker, or add a non-game-file runtime trigger that calls TrackTracker inside the SC process.")
    elif not sc_context["native_launch_hook"] and sc_context["launch_kills_wineserver"]:
        print("diagnosis=SC launch script is missing the native Tobii hook. sc-launch.sh runs wineserver -k, which kills a manually-started Wine SESP pipe helper before the game starts.")
        print("next=run make sc-tobii-stock-install-launch-hook, start the dashboard, then launch Star Citizen normally so services restart after wineserver -k.")
    elif provider_nudge_failed and not provider_nudge_ok and not provider_latch_seen:
        print("diagnosis=SESP init/display-info path is reached, but provider nudge writes fail because the callback pipe closes before adoption.")
        print("next=focus on natural provider discovery/adoption so the stock DLL calls TrackTracker or reaches the 0x32/0x1a feature latch.")
    elif pipe_accept and headpose and not provider_latch_seen:
        if tcp["stream_events"] and not subscribe_seen:
            print("diagnosis=SESP pipe and live headpose are flowing, and TCP can deliver async presence, but SC/stock DLL still did not enter the tracked-provider path.")
            print("next=run make sc-tobii-stock-adoption-kick while SC is open to test whether TrackTracker adoption can be warmed externally; if not, the remaining gate is likely an in-process TrackTracker/API transition.")
        else:
            print("diagnosis=SESP pipe and live headpose are flowing, but SC/stock DLL did not reach the provider status/feature latch.")
            print("next=fix discovery/provider adoption so the DLL advances to runtime metadata, subscriptions, and the 0x32/0x1a feature latch.")
    elif richest and not runtime_seen:
        last = richest["objects"][-1] if richest["objects"] else None
        print(f"diagnosis=SC/stock DLL connected to TCP middleware, probed metadata, then stopped before tracking; last object was {fmt_obj(last) if last is not None else 'none'}.")
        print("next=fix discovery/provider adoption so the DLL advances to runtime metadata and subscriptions.")
    elif runtime_seen and subscribe_seen and pipe_accept == 0:
        print("diagnosis=TCP tracking path advanced, but the DLL did not attach to the SESP pipe.")
        print("next=inspect SESP service pipe visibility/Wine prefix and pipe name.")
    elif pipe_accept and headpose == 0:
        print("diagnosis=SESP pipe attached, but no headpose events reached the stock DLL.")
        print("next=inspect SESP request/response sequence and subscription channel.")
    elif headpose:
        print("diagnosis=headpose path is flowing; remaining issue is likely SC settings, axes, gains, or gaze targeting data.")
    else:
        print("diagnosis=no complete Tobii runtime path yet.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
