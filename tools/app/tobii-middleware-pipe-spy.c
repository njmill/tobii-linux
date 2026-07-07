#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <ctype.h>
#include <math.h>

static const char *log_path = "";
static int reply_bootstrap = 0;
static unsigned char *client_write = NULL;
static DWORD client_write_len = 0;
static int sesp_connect_reply_enabled = 0;
static uint32_t sesp_connect_reply = 1;
static int sesp_auto_reply = 0;
static uint8_t sesp_auto_reply_type = 4;
static char display_name_override[128];
static char display_device_id_override[256];
static uint32_t display_width_override = 0;
static uint32_t display_height_override = 0;
static uint32_t registered_channel = 0;
static char registered_client_name[MAX_PATH];
static int sesp_synthetic_headpose = 0;
static uint32_t sesp_synthetic_headpose_hz = 30;
static uint32_t sesp_headpose_udp_port = 0;
static int sesp_provider_nudge = 0;
static int etdefaultpipe_mode = 0;
static int client_pipe_suffix_scan = 0;
static char etdefaultpipe_entry[512] = "127.0.0.1";

struct PosePacket {
    double x_cm;
    double y_cm;
    double z_cm;
    double yaw_deg;
    double pitch_deg;
    double roll_deg;
    double source_code;
    double packet_counter;
    double gaze_x;
    double gaze_y;
    double gaze_valid;
    double gaze_counter;
};

struct LivePoseState {
    CRITICAL_SECTION lock;
    struct PosePacket pose;
    ULONGLONG last_tick;
    unsigned long long packets;
    int has_pose;
};

static struct LivePoseState live_pose;
static int live_pose_initialized = 0;
static volatile LONG live_pose_udp_running = 0;
static HANDLE live_pose_udp_thread = NULL;

static void log_line(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);

    FILE *f = NULL;
    if (log_path && log_path[0])
        f = fopen(log_path, "ab");

    double now = (double)GetTickCount64() / 1000.0;
    printf("%.3f ", now);
    if (f)
        fprintf(f, "%.3f ", now);

    va_list ap2;
    va_copy(ap2, ap);
    vprintf(fmt, ap);
    printf("\n");
    fflush(stdout);
    if (f) {
        vfprintf(f, fmt, ap2);
        fprintf(f, "\n");
        fclose(f);
    }

    va_end(ap2);
    va_end(ap);
}

static double finite_or_zero(double value)
{
    return isfinite(value) ? value : 0.0;
}

static int snapshot_live_pose(struct PosePacket *out, DWORD max_age_ms)
{
    int fresh = 0;
    if (!live_pose_initialized || !out)
        return 0;

    EnterCriticalSection(&live_pose.lock);
    if (live_pose.has_pose) {
        *out = live_pose.pose;
        fresh = (GetTickCount64() - live_pose.last_tick) <= max_age_ms;
    }
    LeaveCriticalSection(&live_pose.lock);
    return fresh;
}

static DWORD WINAPI live_pose_udp_worker(LPVOID arg)
{
    uint32_t port = *(uint32_t *)arg;
    WSADATA wsa;
    SOCKET sock = INVALID_SOCKET;

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        log_line("live_pose_udp_wsa_failed");
        return 1;
    }

    sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock == INVALID_SOCKET) {
        log_line("live_pose_udp_socket_failed error=%d", WSAGetLastError());
        WSACleanup();
        return 1;
    }

    DWORD timeout_ms = 250;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout_ms, sizeof(timeout_ms));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR) {
        log_line("live_pose_udp_bind_failed port=%lu error=%d", (unsigned long)port, WSAGetLastError());
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    log_line("live_pose_udp_listening port=%lu", (unsigned long)port);
    while (InterlockedCompareExchange(&live_pose_udp_running, 1, 1) == 1) {
        struct PosePacket packet;
        memset(&packet, 0, sizeof(packet));
        int got = recv(sock, (char *)&packet, sizeof(packet), 0);
        if (got == (int)sizeof(packet) || got == (int)(sizeof(double) * 8)) {
            packet.x_cm = finite_or_zero(packet.x_cm);
            packet.y_cm = finite_or_zero(packet.y_cm);
            packet.z_cm = finite_or_zero(packet.z_cm);
            packet.yaw_deg = finite_or_zero(packet.yaw_deg);
            packet.pitch_deg = finite_or_zero(packet.pitch_deg);
            packet.roll_deg = finite_or_zero(packet.roll_deg);
            packet.source_code = finite_or_zero(packet.source_code);
            packet.packet_counter = finite_or_zero(packet.packet_counter);
            packet.gaze_x = finite_or_zero(packet.gaze_x);
            packet.gaze_y = finite_or_zero(packet.gaze_y);
            packet.gaze_valid = finite_or_zero(packet.gaze_valid);
            packet.gaze_counter = finite_or_zero(packet.gaze_counter);

            EnterCriticalSection(&live_pose.lock);
            live_pose.pose = packet;
            live_pose.last_tick = GetTickCount64();
            live_pose.packets++;
            unsigned long long packets = live_pose.packets;
            live_pose.has_pose = 1;
            LeaveCriticalSection(&live_pose.lock);

            if ((packets % 120ULL) == 1ULL) {
                log_line("live_pose_udp_packet packets=%llu yaw=%.2f pitch=%.2f roll=%.2f x=%.2f y=%.2f z=%.2f source=%.0f",
                         packets,
                         packet.yaw_deg,
                         packet.pitch_deg,
                         packet.roll_deg,
                         packet.x_cm,
                         packet.y_cm,
                         packet.z_cm,
                         packet.source_code);
            }
        }
    }

    closesocket(sock);
    WSACleanup();
    log_line("live_pose_udp_stopped");
    return 0;
}

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

static unsigned char *hex_decode(const char *hex, DWORD *out_len)
{
    size_t n = strlen(hex);
    unsigned char *out = (unsigned char *)calloc(n / 2 + 1, 1);
    DWORD len = 0;
    int hi = -1;
    if (!out)
        return NULL;

    for (size_t i = 0; i < n; ++i) {
        int v = hex_nibble(hex[i]);
        if (v < 0)
            continue;
        if (hi < 0)
            hi = v;
        else {
            out[len++] = (unsigned char)((hi << 4) | v);
            hi = -1;
        }
    }
    *out_len = len;
    return out;
}

static int set_client_write_hex(const char *hex)
{
    free(client_write);
    client_write = hex_decode(hex, &client_write_len);
    return client_write != NULL;
}

static int set_client_write_text(const char *text)
{
    size_t len = strlen(text);
    unsigned char *buf = (unsigned char *)calloc(len + 1, 1);
    if (!buf)
        return 0;
    memcpy(buf, text, len);
    free(client_write);
    client_write = buf;
    client_write_len = (DWORD)(len + 1);
    return 1;
}

static uint32_t read_be32(const unsigned char *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static uint32_t read_le32(const unsigned char *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void write_be32(unsigned char *p, uint32_t v)
{
    p[0] = (unsigned char)(v >> 24);
    p[1] = (unsigned char)(v >> 16);
    p[2] = (unsigned char)(v >> 8);
    p[3] = (unsigned char)v;
}

static void write_le32(unsigned char *p, uint32_t v)
{
    p[0] = (unsigned char)v;
    p[1] = (unsigned char)(v >> 8);
    p[2] = (unsigned char)(v >> 16);
    p[3] = (unsigned char)(v >> 24);
}

static void write_le_float(unsigned char *p, float v)
{
    uint32_t raw = 0;
    memcpy(&raw, &v, sizeof(raw));
    write_le32(p, raw);
}

static void write_le16(unsigned char *p, uint16_t v)
{
    p[0] = (unsigned char)v;
    p[1] = (unsigned char)(v >> 8);
}

static void log_hex(const char *prefix, const unsigned char *data, DWORD len)
{
    FILE *f = NULL;
    if (log_path && log_path[0])
        f = fopen(log_path, "ab");

    double now = (double)GetTickCount64() / 1000.0;
    printf("%.3f %s bytes=%lu hex=", now, prefix, (unsigned long)len);
    if (f)
        fprintf(f, "%.3f %s bytes=%lu hex=", now, prefix, (unsigned long)len);
    for (DWORD i = 0; i < len; ++i) {
        printf("%02x", data[i]);
        if (f)
            fprintf(f, "%02x", data[i]);
    }
    printf("\n");
    fflush(stdout);
    if (f) {
        fprintf(f, "\n");
        fclose(f);
    }
}

static void drain_service_pipe(HANDLE pipe)
{
    unsigned char buf[65536];
    DWORD total = 0;
    registered_channel = 0;
    registered_client_name[0] = '\0';

    for (;;) {
        DWORD to_read = sizeof(buf);
        DWORD got = 0;
        BOOL ok = ReadFile(pipe, buf, to_read, &got, NULL);
        if (!ok || !got) {
            DWORD err = GetLastError();
            if (total && (err == ERROR_BROKEN_PIPE || err == ERROR_NO_DATA))
                return;
            log_line("service_pipe_read_failed error=%lu got=%lu", err, (unsigned long)got);
            return;
        }
        total += got;
        log_hex("service_pipe_recv", buf, got);
        if (got >= 6) {
            const char *client_name = (const char *)(buf + 4);
            DWORD name_room = got - 4;
            DWORD name_len = 0;
            while (name_len < name_room && client_name[name_len] != '\0')
                ++name_len;
            if (name_len < name_room) {
                registered_channel = read_le32(buf);
                snprintf(registered_client_name, sizeof(registered_client_name), "%s", client_name);
                log_line(
                    "service_pipe_register channel=0x%04lx client_name=%s extra_bytes=%lu",
                    (unsigned long)registered_channel,
                    registered_client_name,
                    (unsigned long)(name_room - name_len - 1));
            }
        }
        if (got < sizeof(buf))
            return;
    }
}

static void write_client_payload(HANDLE client, uint32_t channel)
{
    if (client_write && client_write_len) {
        DWORD wrote = 0;
        BOOL ok = WriteFile(client, client_write, client_write_len, &wrote, NULL);
        log_hex(ok ? "client_pipe_write" : "client_pipe_write_failed", client_write, wrote);
        if (!ok)
            log_line("client_pipe_write_error error=%lu", GetLastError());
        return;
    }

    if (sesp_connect_reply_enabled && channel >= 0x2711 && channel <= 0x2714) {
        unsigned char reply[4];
        reply[0] = (unsigned char)sesp_connect_reply;
        reply[1] = (unsigned char)(sesp_connect_reply >> 8);
        reply[2] = (unsigned char)(sesp_connect_reply >> 16);
        reply[3] = (unsigned char)(sesp_connect_reply >> 24);
        DWORD wrote = 0;
        BOOL ok = WriteFile(client, reply, sizeof(reply), &wrote, NULL);
        log_hex(ok ? "client_pipe_sesp_connect_reply" : "client_pipe_sesp_connect_reply_failed", reply, wrote);
        if (!ok)
            log_line("client_pipe_write_error error=%lu", GetLastError());
    }
}

static int read_flatbuf_root_type(const unsigned char *payload, DWORD payload_len, uint32_t *seq, uint8_t *type)
{
    if (payload_len < 24)
        return 0;
    uint32_t root_off = read_le32(payload);
    if (root_off > payload_len - 16)
        return 0;
    uint32_t obj = root_off;
    int32_t soff = (int32_t)read_le32(payload + obj);
    int64_t vt64 = (int64_t)obj - (int64_t)soff;
    if (vt64 < 0 || vt64 + 10 > payload_len)
        return 0;

    const unsigned char *vt = payload + vt64;
    uint16_t vt_len = (uint16_t)vt[0] | ((uint16_t)vt[1] << 8);
    if (vt_len < 10 || vt64 + vt_len > payload_len)
        return 0;

    uint16_t seq_off = (uint16_t)vt[4] | ((uint16_t)vt[5] << 8);
    uint16_t type_off = (uint16_t)vt[6] | ((uint16_t)vt[7] << 8);
    if (!seq_off || !type_off || obj + seq_off + 4 > payload_len || obj + type_off >= payload_len)
        return 0;

    *seq = read_le32(payload + obj + seq_off);
    *type = payload[obj + type_off];
    return 1;
}

static unsigned char *make_sesp_frame(const unsigned char *payload, uint32_t payload_len, DWORD *out_len)
{
    DWORD frame_len = 12 + payload_len;
    unsigned char *out = (unsigned char *)calloc(frame_len, 1);
    if (!out)
        return NULL;

    memcpy(out, "sesp", 4);
    write_le32(out + 4, payload_len);
    write_le32(out + 8, payload_len ^ 0x70736573U);
    memcpy(out + 12, payload, payload_len);

    *out_len = frame_len;
    return out;
}

static unsigned char *make_sesp_response_initialize(uint32_t seq, DWORD *out_len)
{
    /*
     * Tobii SESP response_initialize, reconstructed from ses_windows.dll:
     *   wrapper table: seq, type=3, payload table
     *   payload table: status enum default success + 15 capability bools.
     *
     * Setting the advertised capability flags true is intentionally optimistic
     * for discovery. Later start requests still decide which streams we emit.
     */
    const uint32_t payload_len = 76;
    const uint32_t wrapper_obj = 4;
    const uint32_t inner_obj = 20;
    const uint32_t inner_vt = 40;
    const uint32_t wrapper_vt = 66;
    unsigned char payload[76];
    memset(payload, 0, sizeof(payload));

    write_le32(payload + 0, wrapper_obj);

    write_le32(payload + wrapper_obj + 0, (uint32_t)(int32_t)((int32_t)wrapper_obj - (int32_t)wrapper_vt));
    write_le32(payload + wrapper_obj + 4, seq);
    write_le32(payload + wrapper_obj + 8, inner_obj - (wrapper_obj + 8));
    payload[wrapper_obj + 12] = 3;

    write_le32(payload + inner_obj + 0, (uint32_t)(int32_t)((int32_t)inner_obj - (int32_t)inner_vt));
    for (uint32_t i = 1; i <= 15; ++i)
        payload[inner_obj + 3 + i] = 1;

    write_le16(payload + inner_vt + 0, 36);
    write_le16(payload + inner_vt + 2, 20);
    write_le16(payload + inner_vt + 4, 0);
    for (uint16_t i = 1; i <= 15; ++i)
        write_le16(payload + inner_vt + 4 + i * 2, (uint16_t)(3 + i));

    write_le16(payload + wrapper_vt + 0, 10);
    write_le16(payload + wrapper_vt + 2, 13);
    write_le16(payload + wrapper_vt + 4, 4);
    write_le16(payload + wrapper_vt + 6, 12);
    write_le16(payload + wrapper_vt + 8, 8);

    return make_sesp_frame(payload, payload_len, out_len);
}

static unsigned char *make_sesp_status_response(uint32_t seq, uint8_t reply_type, DWORD *out_len)
{
    const uint32_t payload_len = 42;
    const uint32_t wrapper_obj = 4;
    const uint32_t inner_obj = 20;
    const uint32_t inner_vt = 24;
    const uint32_t wrapper_vt = 32;
    unsigned char payload[42];
    memset(payload, 0, sizeof(payload));

    write_le32(payload + 0, wrapper_obj);

    write_le32(payload + wrapper_obj + 0, (uint32_t)(int32_t)((int32_t)wrapper_obj - (int32_t)wrapper_vt));
    write_le32(payload + wrapper_obj + 4, seq);
    write_le32(payload + wrapper_obj + 8, inner_obj - (wrapper_obj + 8));
    payload[wrapper_obj + 12] = reply_type;

    write_le32(payload + inner_obj + 0, (uint32_t)(int32_t)((int32_t)inner_obj - (int32_t)inner_vt));

    write_le16(payload + inner_vt + 0, 6);
    write_le16(payload + inner_vt + 2, 4);
    write_le16(payload + inner_vt + 4, 0);

    write_le16(payload + wrapper_vt + 0, 10);
    write_le16(payload + wrapper_vt + 2, 13);
    write_le16(payload + wrapper_vt + 4, 4);
    write_le16(payload + wrapper_vt + 6, 12);
    write_le16(payload + wrapper_vt + 8, 8);

    return make_sesp_frame(payload, payload_len, out_len);
}

static unsigned char *make_sesp_subscription_headpose(
    uint32_t seq,
    uint64_t timestamp,
    float pos_x,
    float pos_y,
    float pos_z,
    float rot_x,
    float rot_y,
    float rot_z,
    DWORD *out_len)
{
    /*
     * ses_windows.dll FUN_1800bb490/FUN_1800b6eb0 encodes
     * sesp_subscription_headpose as wrapper type 8 with a 12-field payload:
     *   0 timestamp high u32, 1 timestamp low u32,
     *   2..4 position xyz floats, 5..7 rotation xyz floats,
     *   8 position-valid int, 9..11 rotation-valid ints.
     *
     * The real FlatBuffer builder omits fields with default values. We include
     * all fields explicitly so the first synthetic probe is easy to inspect.
     */
    const uint32_t wrapper_obj = 4;
    const uint32_t inner_obj = 20;
    const uint32_t inner_obj_size = 52;
    const uint32_t inner_vt = inner_obj + inner_obj_size;
    const uint32_t wrapper_vt = inner_vt + 28;
    const uint32_t payload_len = wrapper_vt + 10;
    unsigned char payload[110];
    memset(payload, 0, sizeof(payload));

    write_le32(payload + 0, wrapper_obj);

    write_le32(payload + wrapper_obj + 0, (uint32_t)(int32_t)((int32_t)wrapper_obj - (int32_t)wrapper_vt));
    write_le32(payload + wrapper_obj + 4, seq);
    write_le32(payload + wrapper_obj + 8, inner_obj - (wrapper_obj + 8));
    payload[wrapper_obj + 12] = 8;

    write_le32(payload + inner_obj + 0, (uint32_t)(int32_t)((int32_t)inner_obj - (int32_t)inner_vt));
    write_le32(payload + inner_obj + 4, (uint32_t)(timestamp >> 32));
    write_le32(payload + inner_obj + 8, (uint32_t)timestamp);
    write_le_float(payload + inner_obj + 12, pos_x);
    write_le_float(payload + inner_obj + 16, pos_y);
    write_le_float(payload + inner_obj + 20, pos_z);
    write_le_float(payload + inner_obj + 24, rot_x);
    write_le_float(payload + inner_obj + 28, rot_y);
    write_le_float(payload + inner_obj + 32, rot_z);
    write_le32(payload + inner_obj + 36, 1);
    write_le32(payload + inner_obj + 40, 1);
    write_le32(payload + inner_obj + 44, 1);
    write_le32(payload + inner_obj + 48, 1);

    write_le16(payload + inner_vt + 0, 28);
    write_le16(payload + inner_vt + 2, inner_obj_size);
    for (uint16_t i = 0; i < 12; ++i)
        write_le16(payload + inner_vt + 4 + i * 2, (uint16_t)(4 + i * 4));

    write_le16(payload + wrapper_vt + 0, 10);
    write_le16(payload + wrapper_vt + 2, 13);
    write_le16(payload + wrapper_vt + 4, 4);
    write_le16(payload + wrapper_vt + 6, 12);
    write_le16(payload + wrapper_vt + 8, 8);

    return make_sesp_frame(payload, payload_len, out_len);
}

struct SyntheticHeadposeWriter {
    HANDLE client;
    volatile LONG running;
};

static DWORD WINAPI synthetic_headpose_worker(LPVOID arg)
{
    struct SyntheticHeadposeWriter *writer = (struct SyntheticHeadposeWriter *)arg;
    uint32_t seq = 1;
    int phase = 0;
    int live_was_active = 0;
    DWORD sleep_ms = sesp_synthetic_headpose_hz ? (DWORD)(1000U / sesp_synthetic_headpose_hz) : 33;
    if (sleep_ms < 5)
        sleep_ms = 5;

    log_line("sesp_synthetic_headpose_start hz=%lu", (unsigned long)sesp_synthetic_headpose_hz);
    while (InterlockedCompareExchange(&writer->running, 1, 1) == 1) {
        int saw = phase % 120;
        float centered = (float)(saw < 60 ? saw : 120 - saw) / 60.0f;
        float yaw = (centered - 0.5f) * 0.30f;
        float pitch = (centered - 0.5f) * 0.16f;
        float roll = (centered - 0.5f) * 0.08f;
        float pos_x = 0.0f;
        float pos_y = 0.0f;
        float pos_z = 600.0f;
        struct PosePacket live;
        int live_active = snapshot_live_pose(&live, 750);
        if (live_active) {
            const double deg_to_rad = 3.14159265358979323846 / 180.0;
            pos_x = (float)(live.x_cm * 10.0);
            pos_y = (float)(live.y_cm * 10.0);
            pos_z = (float)(600.0 + live.z_cm * 10.0);
            yaw = (float)(live.yaw_deg * deg_to_rad);
            pitch = (float)(live.pitch_deg * deg_to_rad);
            roll = (float)(live.roll_deg * deg_to_rad);
            if (!live_was_active)
                log_line("sesp_headpose_live_source active port=%lu", (unsigned long)sesp_headpose_udp_port);
        } else if (live_was_active) {
            log_line("sesp_headpose_live_source stale fallback=synthetic");
        }
        live_was_active = live_active;
        DWORD frame_len = 0;
        unsigned char *frame = make_sesp_subscription_headpose(
            seq++,
            (uint64_t)GetTickCount64() * 1000ULL,
            pos_x,
            pos_y,
            pos_z,
            pitch,
            yaw,
            roll,
            &frame_len);
        if (frame && frame_len) {
            DWORD wrote = 0;
            BOOL ok = WriteFile(writer->client, frame, frame_len, &wrote, NULL);
            if (!ok) {
                log_line("sesp_synthetic_headpose_write_failed error=%lu seq=%lu", GetLastError(), (unsigned long)(seq - 1));
                free(frame);
                break;
            }
            if ((seq % sesp_synthetic_headpose_hz) == 0)
                log_line("sesp_synthetic_headpose seq=%lu bytes=%lu mode=%s yaw=%.3f pitch=%.3f roll=%.3f",
                         (unsigned long)(seq - 1),
                         (unsigned long)wrote,
                         live_active ? "live" : "synthetic",
                         yaw,
                         pitch,
                         roll);
        }
        free(frame);
        phase++;
        Sleep(sleep_ms);
    }
    log_line("sesp_synthetic_headpose_stop");
    return 0;
}

static void normalize_display_device_id(char *s)
{
    for (; *s; ++s) {
        if (*s == '#')
            *s = '\\';
        else
            *s = (char)toupper((unsigned char)*s);
    }
}

static void choose_display_device_id_substring(char *out, size_t out_len)
{
    if (!out_len)
        return;
    out[0] = '\0';

    if (display_device_id_override[0]) {
        snprintf(out, out_len, "%s", display_device_id_override);
        normalize_display_device_id(out);
        return;
    }

    DISPLAY_DEVICEA adapter;
    DISPLAY_DEVICEA monitor;
    for (DWORD adapter_index = 0; ; ++adapter_index) {
        memset(&adapter, 0, sizeof(adapter));
        adapter.cb = sizeof(adapter);
        if (!EnumDisplayDevicesA(NULL, adapter_index, &adapter, 0))
            break;

        for (DWORD monitor_index = 0; ; ++monitor_index) {
            memset(&monitor, 0, sizeof(monitor));
            monitor.cb = sizeof(monitor);
            if (!EnumDisplayDevicesA(adapter.DeviceName, monitor_index, &monitor, 0))
                break;
            if (!monitor.DeviceID[0])
                continue;

            snprintf(out, out_len, "%s", monitor.DeviceID);
            normalize_display_device_id(out);
            return;
        }
    }

    snprintf(out, out_len, "DISPLAY\\DEFAULT_MONITOR");
}

static unsigned char *make_sesp_display_info_response(uint32_t seq, DWORD *out_len)
{
    char display_name[128];
    char device_id_substring[256];
    uint32_t display_width = display_width_override;
    uint32_t display_height = display_height_override;

    snprintf(display_name, sizeof(display_name), "%s", display_name_override[0] ? display_name_override : "\\\\.\\DISPLAY1");
    if (!display_width)
        display_width = (uint32_t)GetSystemMetrics(SM_CXSCREEN);
    if (!display_height)
        display_height = (uint32_t)GetSystemMetrics(SM_CYSCREEN);

    POINT pt;
    pt.x = 0;
    pt.y = 0;
    HMONITOR mon = MonitorFromPoint(pt, MONITOR_DEFAULTTOPRIMARY);
    MONITORINFOEXA mi;
    memset(&mi, 0, sizeof(mi));
    mi.cbSize = sizeof(mi);
    if (mon && GetMonitorInfoA(mon, (MONITORINFO *)&mi)) {
        if (!display_name_override[0] && mi.szDevice[0])
            snprintf(display_name, sizeof(display_name), "%s", mi.szDevice);
        if (!display_width_override)
            display_width = (uint32_t)(mi.rcMonitor.right - mi.rcMonitor.left);
        if (!display_height_override)
            display_height = (uint32_t)(mi.rcMonitor.bottom - mi.rcMonitor.top);
    }

    /*
     * The stock DLL's response_get_display_info callback consumes:
     *   field 0: status enum, where 0 is success
     *   field 1: display device-id substring
     *   field 2: display width
     *   field 3: display height
     *
     * It searches EnumDisplayDevicesA monitor DeviceID values for the substring,
     * then returns the matching adapter DeviceName (typically "\\.\DISPLAY1").
     * Wine currently reports monitor IDs like:
     *   \\?\DISPLAY#Default_Monitor#0000&0000#{...}
     * and the DLL normalizes them to uppercase with '#' changed to '\'.
     */
    choose_display_device_id_substring(device_id_substring, sizeof(device_id_substring));

    const uint32_t wrapper_obj = 4;
    const uint32_t inner_obj = 20;
    const uint32_t device_id_string = 36;
    const uint32_t device_id_len = (uint32_t)strlen(device_id_substring);
    const uint32_t inner_vt = (device_id_string + 4 + device_id_len + 1 + 3) & ~3U;
    const uint32_t wrapper_vt = inner_vt + 12;
    const uint32_t payload_len = wrapper_vt + 10;
    unsigned char *payload = (unsigned char *)calloc(payload_len, 1);
    if (!payload)
        return NULL;

    write_le32(payload + 0, wrapper_obj);

    write_le32(payload + wrapper_obj + 0, (uint32_t)(int32_t)((int32_t)wrapper_obj - (int32_t)wrapper_vt));
    write_le32(payload + wrapper_obj + 4, seq);
    write_le32(payload + wrapper_obj + 8, inner_obj - (wrapper_obj + 8));
    payload[wrapper_obj + 12] = 10;

    write_le32(payload + inner_obj + 0, (uint32_t)(int32_t)((int32_t)inner_obj - (int32_t)inner_vt));
    write_le32(payload + inner_obj + 4, 0);
    write_le32(payload + inner_obj + 8, device_id_string - (inner_obj + 8));
    write_le32(payload + inner_obj + 12, display_width);
    write_le32(payload + inner_obj + 16, display_height);

    write_le32(payload + device_id_string + 0, device_id_len);
    memcpy(payload + device_id_string + 4, device_id_substring, device_id_len);

    write_le16(payload + inner_vt + 0, 12);
    write_le16(payload + inner_vt + 2, 20);
    write_le16(payload + inner_vt + 4, 4);
    write_le16(payload + inner_vt + 6, 8);
    write_le16(payload + inner_vt + 8, 12);
    write_le16(payload + inner_vt + 10, 16);

    write_le16(payload + wrapper_vt + 0, 10);
    write_le16(payload + wrapper_vt + 2, 13);
    write_le16(payload + wrapper_vt + 4, 4);
    write_le16(payload + wrapper_vt + 6, 12);
    write_le16(payload + wrapper_vt + 8, 8);

    log_line("sesp_display_info name=%s device_id_substring=%s size=%lux%lu",
             display_name,
             device_id_substring,
             (unsigned long)display_width,
             (unsigned long)display_height);

    unsigned char *frame = make_sesp_frame(payload, payload_len, out_len);
    free(payload);
    return frame;
}

static uint32_t put_fb_string(unsigned char *payload, uint32_t pos, const char *s)
{
    uint32_t len = (uint32_t)strlen(s);
    write_le32(payload + pos, len);
    memcpy(payload + pos + 4, s, len);
    return (pos + 4 + len + 1 + 3) & ~3U;
}

static unsigned char *make_sesp_list_devices_response(uint32_t seq, DWORD *out_len)
{
    /*
     * SESP response_list_devices, reconstructed from the stock DLL parser:
     *   wrapper table: seq, type=0x0c, payload table
     *   payload table: vector<device>
     *   device table fields observed by the DLL:
     *     0 url, 1 display/name, 2 model, 3 serial, 4 family, 5 firmware, 6 type enum
     *
     * Later fields carry deeper capability/profile metadata. This first-pass
     * response intentionally fills the strings copied unconditionally by the
     * game-integration layer and leaves optional capability vectors absent.
     */
    const char *url = "tobii-ttp://IS5FF-100203612152";
    const char *name = "Eye Tracker 5";
    const char *model = "IS5_Large_Eyetracker_5";
    const char *serial = "IS5FF-100203612152";
    const char *family = "IS5";
    const char *firmware = "02a1a6a977";

    const uint32_t wrapper_obj = 4;
    const uint32_t inner_obj = 20;
    const uint32_t devices_vector = 28;
    const uint32_t device_obj = 36;
    const uint32_t strings_start = 68;
    unsigned char *payload = (unsigned char *)calloc(512, 1);
    if (!payload)
        return NULL;

    uint32_t pos = strings_start;
    uint32_t url_obj = pos;
    pos = put_fb_string(payload, pos, url);
    uint32_t name_obj = pos;
    pos = put_fb_string(payload, pos, name);
    uint32_t model_obj = pos;
    pos = put_fb_string(payload, pos, model);
    uint32_t serial_obj = pos;
    pos = put_fb_string(payload, pos, serial);
    uint32_t family_obj = pos;
    pos = put_fb_string(payload, pos, family);
    uint32_t firmware_obj = pos;
    pos = put_fb_string(payload, pos, firmware);

    uint32_t device_vt = pos;
    pos += 18;
    uint32_t inner_vt = pos;
    pos += 6;
    uint32_t wrapper_vt = pos;
    pos += 10;
    uint32_t payload_len = pos;

    write_le32(payload + 0, wrapper_obj);

    write_le32(payload + wrapper_obj + 0, (uint32_t)(int32_t)((int32_t)wrapper_obj - (int32_t)wrapper_vt));
    write_le32(payload + wrapper_obj + 4, seq);
    write_le32(payload + wrapper_obj + 8, inner_obj - (wrapper_obj + 8));
    payload[wrapper_obj + 12] = 0x0c;

    write_le32(payload + inner_obj + 0, (uint32_t)(int32_t)((int32_t)inner_obj - (int32_t)inner_vt));
    write_le32(payload + inner_obj + 4, devices_vector - (inner_obj + 4));

    write_le32(payload + devices_vector + 0, 1);
    write_le32(payload + devices_vector + 4, device_obj - (devices_vector + 4));

    write_le32(payload + device_obj + 0, (uint32_t)(int32_t)((int32_t)device_obj - (int32_t)device_vt));
    write_le32(payload + device_obj + 4, url_obj - (device_obj + 4));
    write_le32(payload + device_obj + 8, name_obj - (device_obj + 8));
    write_le32(payload + device_obj + 12, model_obj - (device_obj + 12));
    write_le32(payload + device_obj + 16, serial_obj - (device_obj + 16));
    write_le32(payload + device_obj + 20, family_obj - (device_obj + 20));
    write_le32(payload + device_obj + 24, firmware_obj - (device_obj + 24));
    write_le32(payload + device_obj + 28, 1);

    write_le16(payload + device_vt + 0, 18);
    write_le16(payload + device_vt + 2, 32);
    write_le16(payload + device_vt + 4, 4);
    write_le16(payload + device_vt + 6, 8);
    write_le16(payload + device_vt + 8, 12);
    write_le16(payload + device_vt + 10, 16);
    write_le16(payload + device_vt + 12, 20);
    write_le16(payload + device_vt + 14, 24);
    write_le16(payload + device_vt + 16, 28);

    write_le16(payload + inner_vt + 0, 6);
    write_le16(payload + inner_vt + 2, 8);
    write_le16(payload + inner_vt + 4, 4);

    write_le16(payload + wrapper_vt + 0, 10);
    write_le16(payload + wrapper_vt + 2, 13);
    write_le16(payload + wrapper_vt + 4, 4);
    write_le16(payload + wrapper_vt + 6, 12);
    write_le16(payload + wrapper_vt + 8, 8);

    log_line("sesp_list_devices count=1 url=%s model=%s serial=%s", url, model, serial);

    unsigned char *frame = make_sesp_frame(payload, payload_len, out_len);
    free(payload);
    return frame;
}

static unsigned char *make_sesp_reply_from_request(const unsigned char *frame, DWORD frame_len, uint8_t fallback_reply_type, DWORD *out_len, uint8_t *out_req_type)
{
    if (frame_len < 32 || memcmp(frame, "sesp", 4) != 0)
        return NULL;

    uint32_t payload_len = read_le32(frame + 4);
    if (payload_len > frame_len - 12)
        return NULL;

    const unsigned char *payload = frame + 12;
    uint32_t seq = 0;
    uint8_t req_type = 0;
    if (!read_flatbuf_root_type(payload, payload_len, &seq, &req_type))
        return NULL;
    if (out_req_type)
        *out_req_type = req_type;
    log_line("sesp_frame_decode seq=%lu type=%u payload_len=%lu", (unsigned long)seq, (unsigned)req_type, (unsigned long)payload_len);

    if (req_type == 2) {
        log_line("sesp_auto_reply seq=%lu request_type=%u reply_type=3 initialize", (unsigned long)seq, (unsigned)req_type);
        return make_sesp_response_initialize(seq, out_len);
    }
    if (req_type == 9) {
        log_line("sesp_auto_reply seq=%lu request_type=%u reply_type=10 display_info", (unsigned long)seq, (unsigned)req_type);
        return make_sesp_display_info_response(seq, out_len);
    }
    if (req_type == 0xb) {
        log_line("sesp_auto_reply seq=%lu request_type=%u reply_type=12 list_devices", (unsigned long)seq, (unsigned)req_type);
        return make_sesp_list_devices_response(seq, out_len);
    }
    if (req_type == 4 || req_type == 6 || req_type == 0xd ||
        req_type == 0x10 || req_type == 0x14 || req_type == 0x16 ||
        req_type == 0x1a || req_type == 0x21 || req_type == 0x23 ||
        req_type == 0x32) {
        uint8_t reply_type = (uint8_t)(req_type + 1);
        log_line("sesp_auto_reply seq=%lu request_type=%u reply_type=%u status", (unsigned long)seq, (unsigned)req_type, (unsigned)reply_type);
        return make_sesp_status_response(seq, reply_type, out_len);
    }

    log_line("sesp_auto_reply seq=%lu request_type=%u reply_type=%u fallback", (unsigned long)seq, (unsigned)req_type, (unsigned)fallback_reply_type);
    return make_sesp_status_response(seq, fallback_reply_type, out_len);
}

static void write_provider_nudge(HANDLE client, uint32_t base_seq)
{
    uint8_t types[] = { 51, 27, 5 };
    for (unsigned i = 0; i < sizeof(types) / sizeof(types[0]); ++i) {
        DWORD out_len = 0;
        unsigned char *out = make_sesp_status_response(base_seq + i, types[i], &out_len);
        if (out && out_len) {
            DWORD wrote = 0;
            BOOL ok = WriteFile(client, out, out_len, &wrote, NULL);
            log_hex(ok ? "client_pipe_sesp_provider_nudge" : "client_pipe_sesp_provider_nudge_failed", out, wrote);
            if (!ok)
                log_line("client_pipe_sesp_provider_nudge_error error=%lu", GetLastError());
            FlushFileBuffers(client);
        }
        free(out);
        Sleep(20);
    }
}

static void maybe_write_sesp_reply(HANDLE client, const unsigned char *buf, DWORD got)
{
    if (!sesp_auto_reply)
        return;

    DWORD offset = 0;
    while (offset + 12 <= got) {
        if (memcmp(buf + offset, "sesp", 4) != 0)
            return;
        uint32_t payload_len = read_le32(buf + offset + 4);
        DWORD frame_len = 12 + payload_len;
        if (frame_len > got - offset)
            return;

        DWORD out_len = 0;
        uint8_t req_type = 0;
        unsigned char *out = make_sesp_reply_from_request(buf + offset, frame_len, sesp_auto_reply_type, &out_len, &req_type);
        if (out && out_len) {
            DWORD wrote = 0;
            BOOL ok = WriteFile(client, out, out_len, &wrote, NULL);
            log_hex(ok ? "client_pipe_sesp_auto_reply" : "client_pipe_sesp_auto_reply_failed", out, wrote);
            if (!ok)
                log_line("client_pipe_sesp_auto_reply_error error=%lu", GetLastError());
            FlushFileBuffers(client);
        }
        free(out);
        if (sesp_provider_nudge && req_type == 2) {
            log_line("sesp_provider_nudge after_initialize");
            write_provider_nudge(client, 0x6000);
        }
        if (sesp_provider_nudge && req_type == 9) {
            log_line("sesp_provider_nudge after_display_info");
            write_provider_nudge(client, 0x7000);
        }
        offset += frame_len;
    }
}

static void serve_service_followup(HANDLE service_pipe, HANDLE client, uint32_t channel)
{
    unsigned char buf[65536];
    DWORD total = 0;

    for (;;) {
        DWORD got = 0;
        BOOL ok = ReadFile(service_pipe, buf, sizeof(buf), &got, NULL);
        if (!ok || !got) {
            log_line(
                "service_pipe_close channel=0x%04lx rx_followup=%lu error=%lu",
                (unsigned long)channel,
                (unsigned long)total,
                GetLastError());
            return;
        }
        total += got;
        log_hex("service_pipe_recv_followup", buf, got);
        maybe_write_sesp_reply(client, buf, got);
    }
}

static const char *payload_for_object(uint32_t obj)
{
    switch (obj) {
    case 0x03e8:
        return "0000020000000400010008";
    case 0x0640:
        return "0000020000000400000000020000000400000000020000000400000000";
    case 0x076c:
    case 0x04c4:
        return "";
    case 0x058c:
        return
            "000014000000160000001249533546462d313030323033363132313532"
            "140000001a000000164953355f4c617267655f457965747261636b65725f35"
            "140000000700000003495335"
            "140000000e0000000a30326131613661393737";
    case 0x0532:
        return
            "0000050000000400022710020000000400000000140000000e0000000a5065726970686572616c"
            "05000000040002271002000000040000000114000000080000000474727565"
            "050000000400022710020000000400000002140000000d0000000948454c4c4f5f4e4952"
            "05000000040002271002000000040000000314000000130000000f4953354c455945545241434b455235"
            "05000000040002271002000000040000000514000000090000000566616c7365"
            "05000000040002271002000000040000000614000000090000000566616c7365"
            "05000000040002271002000000040000000714000000090000000566616c7365"
            "05000000040002271002000000040000000c14000000050000000134";
    case 0x05d2:
        return
            "000005000000040002271002000000040000000014000000090000000566616c7365"
            "05000000040002271002000000040000000114000000090000000566616c7365"
            "05000000040002271002000000040000000214000000090000000566616c7365"
            "05000000040002271002000000040000000314000000090000000566616c7365"
            "050000000400022710020000000400000004140000000400000000"
            "0500000004000227100200000004000000051400000006000000026f6b"
            "0500000004000227100200000004000000061400000006000000026f6b"
            "05000000040002271002000000040000000714000000050000000130"
            "05000000040002271002000000040000000814000000090000000566616c7365";
    case 0x0546:
        return
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
            "05000000040002271002000000040000000c14000000090000000566616c7365";
    case 0x04b0:
        return
            "00000500000004000a0100020000000400001389"
            "05000000040004138902000000040000050014000000080000000467617a65"
            "140000000400000000020000000400000000"
            "050000000400041389020000000400000501140000000900000005696d616765"
            "140000000400000000020000000400000000"
            "050000000400041389020000000400000504140000000c0000000870726573656e6365"
            "140000000400000000020000000400000000"
            "050000000400041389020000000400000508140000001400000010696d6167655f636f6c6c656374696f6e"
            "1400000004000000000200000004000003e8"
            "05000000040004138902000000040000050e1400000018000000147072696d6172795f63616d6572615f696d616765"
            "140000000400000000020000000400000000"
            "050000000400041389020000000400001770140000000b00000007616c676f646267"
            "140000000400000000020000000400000000"
            "05000000040004138902000000040000177114000000130000000f6973355f73796e635f73747265616d"
            "140000000400000000020000000400000000"
            "0500000004000413890200000004000017721400000007000000036c6f67"
            "140000000400000000020000000400000000"
            "050000000400041389020000000400001774140000000a00000006637573746f6d"
            "140000000400000000020000000400000000";
    case 0x0596:
        return
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
            "020000000400003039";
    case 0x05b4:
        return "0000020000000400000001";
    case 0x06a4:
        return "0000140000001a000000164953355f4c617267655f457965747261636b65725f35";
    case 0x0bf4:
        return "00001a0000000400000000";
    case 0x083e:
        return
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
            "040000000800002770a3d70a3d";
    default:
        return NULL;
    }
}

static unsigned char *make_ttp_response(const unsigned char *req, DWORD req_len, DWORD *out_len)
{
    if (req_len < 24 || read_be32(req) != 0x51)
        return NULL;

    uint32_t seq = read_be32(req + 4);
    uint32_t obj = read_be32(req + 12);
    const char *payload_hex = payload_for_object(obj);
    if (!payload_hex)
        return NULL;

    DWORD payload_len = 0;
    unsigned char *payload = hex_decode(payload_hex, &payload_len);
    unsigned char *out = (unsigned char *)calloc(24 + payload_len, 1);
    if (!out) {
        free(payload);
        return NULL;
    }
    write_be32(out, 0x52);
    write_be32(out + 4, seq);
    write_be32(out + 8, 1);
    write_be32(out + 12, obj);
    write_be32(out + 16, 0);
    write_be32(out + 20, payload_len);
    if (payload_len)
        memcpy(out + 24, payload, payload_len);
    free(payload);
    *out_len = 24 + payload_len;
    return out;
}

static void __attribute__((unused)) serve_pipe_once(HANDLE pipe)
{
    unsigned char buf[65536];
    DWORD total_rx = 0, total_tx = 0;

    for (;;) {
        DWORD got = 0;
        BOOL ok = ReadFile(pipe, buf, sizeof(buf), &got, NULL);
        if (!ok || got == 0) {
            log_line("pipe_close rx=%lu tx=%lu last_error=%lu", (unsigned long)total_rx, (unsigned long)total_tx, GetLastError());
            break;
        }
        total_rx += got;
        log_hex("pipe_recv", buf, got);

        if (reply_bootstrap) {
            DWORD out_len = 0;
            unsigned char *out = make_ttp_response(buf, got, &out_len);
            if (out && out_len) {
                DWORD wrote = 0;
                WriteFile(pipe, out, out_len, &wrote, NULL);
                total_tx += wrote;
                log_hex("pipe_send", out, wrote);
            }
            free(out);
        }
    }
}

static void serve_etdefaultpipe_once(HANDLE pipe)
{
    unsigned char request[64];
    unsigned char response[8 + 0x200];
    DWORD got = 0;
    DWORD wrote = 0;
    uint32_t request_value = 0;

    memset(request, 0, sizeof(request));
    memset(response, 0, sizeof(response));

    BOOL ok = ReadFile(pipe, request, sizeof(request), &got, NULL);
    if (!ok || got == 0) {
        log_line("etdefaultpipe_read_failed error=%lu got=%lu", GetLastError(), (unsigned long)got);
        return;
    }

    if (got >= 4)
        request_value = read_le32(request);
    log_hex("etdefaultpipe_recv", request, got);
    log_line("etdefaultpipe_request value=%lu entry=%s", (unsigned long)request_value, etdefaultpipe_entry);

    /*
     * The stock DLL's ETDefaultPIPE path calls CallNamedPipeA with a 4-byte
     * request and expects:
     *   u32 status/version == 1
     *   u32 count
     *   count fixed 0x200-byte NUL-terminated entries
     *
     * The DLL prepends "tobii-ttp://" to each returned entry before feeding the
     * URL into its normal tracker-discovery callback.
     */
    write_le32(response + 0, 1);
    write_le32(response + 4, 1);
    snprintf((char *)(response + 8), 0x200, "%s", etdefaultpipe_entry);

    ok = WriteFile(pipe, response, sizeof(response), &wrote, NULL);
    log_hex(ok ? "etdefaultpipe_send" : "etdefaultpipe_send_failed", response, wrote);
    if (!ok)
        log_line("etdefaultpipe_write_failed error=%lu", GetLastError());
    FlushFileBuffers(pipe);
}

typedef BOOL(WINAPI *GetNamedPipeClientProcessIdFn)(HANDLE Pipe, PULONG ClientProcessId);

static int connect_client_pipe(HANDLE service_pipe)
{
    int handled = 0;
    ULONGLONG deadline = GetTickCount64() + 1000;
    ULONG client_pid = 0;

    HMODULE kernel32 = GetModuleHandleA("kernel32.dll");
    union {
        FARPROC raw;
        GetNamedPipeClientProcessIdFn typed;
    } get_client_pid_proc = {0};
    if (kernel32)
        get_client_pid_proc.raw = GetProcAddress(kernel32, "GetNamedPipeClientProcessId");
    GetNamedPipeClientProcessIdFn get_client_pid = get_client_pid_proc.typed;
    if (get_client_pid && get_client_pid(service_pipe, &client_pid))
        log_line("client_pipe_pid pid=0x%08lx", (unsigned long)client_pid);
    else
        log_line("client_pipe_pid unavailable error=%lu", GetLastError());

    drain_service_pipe(service_pipe);
    uint32_t channel = registered_channel;
    char client_name[MAX_PATH];
    snprintf(client_name, sizeof(client_name), "%s", registered_client_name);

    if (client_name[0]) {
        char name[MAX_PATH];
        snprintf(name, sizeof(name), "\\\\.\\pipe\\%s", client_name);
        HANDLE client = CreateFileA(
            name,
            GENERIC_WRITE,
            0,
            NULL,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            NULL);
        if (client != INVALID_HANDLE_VALUE) {
            struct SyntheticHeadposeWriter synthetic_writer;
            HANDLE synthetic_thread = NULL;
            log_line("client_pipe_connected_registered name=%s channel=0x%04lx", name, (unsigned long)channel);
            write_client_payload(client, channel);
            FlushFileBuffers(client);
            if (sesp_synthetic_headpose && channel == 0x2712) {
                synthetic_writer.client = client;
                synthetic_writer.running = 1;
                synthetic_thread = CreateThread(NULL, 0, synthetic_headpose_worker, &synthetic_writer, 0, NULL);
                if (!synthetic_thread)
                    log_line("sesp_synthetic_headpose_thread_failed error=%lu", GetLastError());
            }
            serve_service_followup(service_pipe, client, channel);
            if (synthetic_thread) {
                InterlockedExchange(&synthetic_writer.running, 0);
                WaitForSingleObject(synthetic_thread, 1000);
                CloseHandle(synthetic_thread);
            }
            CloseHandle(client);
            return 1;
        }
        log_line("client_pipe_registered_open_failed name=%s error=%lu", name, GetLastError());
    }

    if (client_pipe_suffix_scan && client_pid) {
        DWORD last_open_error = 0;
        ULONGLONG brute_deadline = GetTickCount64() + 1200;
        while (!handled && GetTickCount64() < brute_deadline) {
            for (unsigned suffix = 0; !handled && suffix <= 0xff; ++suffix) {
                char name[MAX_PATH];
                snprintf(name, sizeof(name), "\\\\.\\pipe\\client_streamengineservices_%08lx_00006ffffee900%02x",
                         (unsigned long)client_pid, suffix);
                HANDLE client = CreateFileA(
                    name,
                    GENERIC_WRITE,
                    0,
                    NULL,
                    OPEN_EXISTING,
                    FILE_ATTRIBUTE_NORMAL,
                    NULL);
                if (client != INVALID_HANDLE_VALUE) {
                    struct SyntheticHeadposeWriter synthetic_writer;
                    HANDLE synthetic_thread = NULL;
                    log_line("client_pipe_connected_bruteforce name=%s", name);
                    write_client_payload(client, channel);
                    FlushFileBuffers(client);
                    if (sesp_synthetic_headpose && channel == 0x2712) {
                        synthetic_writer.client = client;
                        synthetic_writer.running = 1;
                        synthetic_thread = CreateThread(NULL, 0, synthetic_headpose_worker, &synthetic_writer, 0, NULL);
                        if (!synthetic_thread)
                            log_line("sesp_synthetic_headpose_thread_failed error=%lu", GetLastError());
                    }
                    serve_service_followup(service_pipe, client, channel);
                    if (synthetic_thread) {
                        InterlockedExchange(&synthetic_writer.running, 0);
                        WaitForSingleObject(synthetic_thread, 1000);
                        CloseHandle(synthetic_thread);
                    }
                    CloseHandle(client);
                    handled = 1;
                    break;
                }
                last_open_error = GetLastError();
            }
            if (!handled)
                Sleep(25);
        }
        if (handled)
            return handled;
        log_line("client_pipe_bruteforce none pid=0x%08lx last_error=%lu", (unsigned long)client_pid, last_open_error);
    } else if (client_pid) {
        log_line("client_pipe_bruteforce disabled pid=0x%08lx", (unsigned long)client_pid);
    }

    while (client_pipe_suffix_scan && !handled && GetTickCount64() < deadline) {
        WIN32_FIND_DATAW find_data;
        HANDLE find = FindFirstFileW(L"\\\\.\\pipe\\*", &find_data);
        if (find == INVALID_HANDLE_VALUE) {
            log_line("client_pipe_find none error=%lu", GetLastError());
            Sleep(25);
            continue;
        }

        do {
            char file_name[MAX_PATH];
            WideCharToMultiByte(CP_UTF8, 0, find_data.cFileName, -1, file_name, sizeof(file_name), NULL, NULL);
            if (strncmp(file_name, "client_streamengineservices_", 28) != 0)
                continue;

            char name[MAX_PATH];
            snprintf(name, sizeof(name), "\\\\.\\pipe\\%s", file_name);
            log_line("client_pipe_candidate name=%s", name);

            HANDLE client = CreateFileA(
                name,
                GENERIC_WRITE,
                0,
                NULL,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                NULL);
            if (client != INVALID_HANDLE_VALUE) {
                struct SyntheticHeadposeWriter synthetic_writer;
                HANDLE synthetic_thread = NULL;
                log_line("client_pipe_connected name=%s", name);
                write_client_payload(client, channel);
                FlushFileBuffers(client);
                if (sesp_synthetic_headpose && channel == 0x2712) {
                    synthetic_writer.client = client;
                    synthetic_writer.running = 1;
                    synthetic_thread = CreateThread(NULL, 0, synthetic_headpose_worker, &synthetic_writer, 0, NULL);
                    if (!synthetic_thread)
                        log_line("sesp_synthetic_headpose_thread_failed error=%lu", GetLastError());
                }
                serve_service_followup(service_pipe, client, channel);
                if (synthetic_thread) {
                    InterlockedExchange(&synthetic_writer.running, 0);
                    WaitForSingleObject(synthetic_thread, 1000);
                    CloseHandle(synthetic_thread);
                }
                CloseHandle(client);
                handled = 1;
                break;
            }
            log_line("client_pipe_open_failed name=%s error=%lu", name, GetLastError());
        } while (FindNextFileW(find, &find_data));

        FindClose(find);
        if (!handled)
            Sleep(25);
    }

    if (!handled)
        log_line("client_pipe_connect none suffix_scan=%s", client_pipe_suffix_scan ? "on" : "off");
    return handled;
}

static DWORD WINAPI service_worker(LPVOID arg)
{
    HANDLE pipe = (HANDLE)arg;
    log_line("pipe_accept");
    if (etdefaultpipe_mode)
        serve_etdefaultpipe_once(pipe);
    else
        connect_client_pipe(pipe);
    DisconnectNamedPipe(pipe);
    CloseHandle(pipe);
    return 0;
}

int main(int argc, char **argv)
{
    DWORD seconds = 35;
    const char *pipe_name = "\\\\.\\pipe\\streamengineservices";
    const char *suffix_scan_env = getenv("SC_TOBII_PIPE_SUFFIX_SCAN");
    if (suffix_scan_env && suffix_scan_env[0] && strcmp(suffix_scan_env, "0"))
        client_pipe_suffix_scan = 1;

    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--seconds") && i + 1 < argc)
            seconds = (DWORD)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--log") && i + 1 < argc)
            log_path = argv[++i];
        else if (!strcmp(argv[i], "--reply-bootstrap"))
            reply_bootstrap = 1;
        else if (!strcmp(argv[i], "--client-write-hex") && i + 1 < argc) {
            if (!set_client_write_hex(argv[++i])) {
                fprintf(stderr, "invalid --client-write-hex\n");
                return 2;
            }
        }
        else if (!strcmp(argv[i], "--client-write-text") && i + 1 < argc) {
            if (!set_client_write_text(argv[++i])) {
                fprintf(stderr, "invalid --client-write-text\n");
                return 2;
            }
        }
        else if (!strcmp(argv[i], "--sesp-connect-reply")) {
            sesp_connect_reply_enabled = 1;
            if (i + 1 < argc && argv[i + 1][0] != '-')
                sesp_connect_reply = (uint32_t)strtoul(argv[++i], NULL, 0);
        }
        else if (!strcmp(argv[i], "--sesp-auto-reply")) {
            sesp_auto_reply = 1;
            if (i + 1 < argc && argv[i + 1][0] != '-')
                sesp_auto_reply_type = (uint8_t)strtoul(argv[++i], NULL, 0);
        }
        else if (!strcmp(argv[i], "--display-name") && i + 1 < argc)
            snprintf(display_name_override, sizeof(display_name_override), "%s", argv[++i]);
        else if (!strcmp(argv[i], "--display-device-id") && i + 1 < argc)
            snprintf(display_device_id_override, sizeof(display_device_id_override), "%s", argv[++i]);
        else if (!strcmp(argv[i], "--display-width") && i + 1 < argc)
            display_width_override = (uint32_t)strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "--display-height") && i + 1 < argc)
            display_height_override = (uint32_t)strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "--sesp-synthetic-headpose")) {
            sesp_synthetic_headpose = 1;
            if (i + 1 < argc && argv[i + 1][0] != '-')
                sesp_synthetic_headpose_hz = (uint32_t)strtoul(argv[++i], NULL, 0);
        }
        else if (!strcmp(argv[i], "--sesp-headpose-udp") && i + 1 < argc) {
            sesp_synthetic_headpose = 1;
            sesp_headpose_udp_port = (uint32_t)strtoul(argv[++i], NULL, 0);
        }
        else if (!strcmp(argv[i], "--sesp-provider-nudge"))
            sesp_provider_nudge = 1;
        else if (!strcmp(argv[i], "--etdefaultpipe"))
            etdefaultpipe_mode = 1;
        else if (!strcmp(argv[i], "--client-pipe-suffix-scan"))
            client_pipe_suffix_scan = 1;
        else if (!strcmp(argv[i], "--etdefault-entry") && i + 1 < argc)
            snprintf(etdefaultpipe_entry, sizeof(etdefaultpipe_entry), "%s", argv[++i]);
        else if (!strcmp(argv[i], "--pipe") && i + 1 < argc)
            pipe_name = argv[++i];
    }

    ULONGLONG deadline = GetTickCount64() + (ULONGLONG)seconds * 1000ULL;
    log_line(
        "pipe_listening name=%s seconds=%lu mode=%s reply_bootstrap=%d client_write_len=%lu sesp_connect_reply=%s/0x%lx sesp_auto_reply=%s/%u synthetic_headpose=%s/%lu headpose_udp=%lu provider_nudge=%s suffix_scan=%s etdefault_entry=%s display_override=%s/%lux%lu",
        pipe_name,
        (unsigned long)seconds,
        etdefaultpipe_mode ? "etdefaultpipe" : "streamengineservices",
        reply_bootstrap,
        (unsigned long)client_write_len,
        sesp_connect_reply_enabled ? "on" : "off",
        (unsigned long)sesp_connect_reply,
        sesp_auto_reply ? "on" : "off",
        (unsigned)sesp_auto_reply_type,
        sesp_synthetic_headpose ? "on" : "off",
        (unsigned long)sesp_synthetic_headpose_hz,
        (unsigned long)sesp_headpose_udp_port,
        sesp_provider_nudge ? "on" : "off",
        client_pipe_suffix_scan ? "on" : "off",
        etdefaultpipe_entry,
        display_name_override[0] ? display_name_override : "auto",
        (unsigned long)display_width_override,
        (unsigned long)display_height_override);

    if (sesp_headpose_udp_port) {
        InitializeCriticalSection(&live_pose.lock);
        live_pose_initialized = 1;
        InterlockedExchange(&live_pose_udp_running, 1);
        live_pose_udp_thread = CreateThread(NULL, 0, live_pose_udp_worker, &sesp_headpose_udp_port, 0, NULL);
        if (!live_pose_udp_thread) {
            log_line("live_pose_udp_thread_failed error=%lu", GetLastError());
            InterlockedExchange(&live_pose_udp_running, 0);
        }
    }

    while (GetTickCount64() < deadline) {
        HANDLE pipe = CreateNamedPipeA(
            pipe_name,
            etdefaultpipe_mode ? PIPE_ACCESS_DUPLEX : PIPE_ACCESS_INBOUND,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES,
            65536,
            65536,
            1000,
            NULL);
        if (pipe == INVALID_HANDLE_VALUE) {
            log_line("CreateNamedPipe failed error=%lu", GetLastError());
            Sleep(500);
            continue;
        }

        BOOL connected = ConnectNamedPipe(pipe, NULL) ? TRUE : (GetLastError() == ERROR_PIPE_CONNECTED);
        if (connected) {
            HANDLE thread = CreateThread(NULL, 0, service_worker, pipe, 0, NULL);
            if (thread != NULL) {
                CloseHandle(thread);
                pipe = INVALID_HANDLE_VALUE;
            } else {
                log_line("CreateThread failed error=%lu", GetLastError());
                DisconnectNamedPipe(pipe);
            }
        } else {
            log_line("ConnectNamedPipe failed error=%lu", GetLastError());
        }
        if (pipe != INVALID_HANDLE_VALUE)
            CloseHandle(pipe);
    }

    log_line("pipe_stopped");
    if (live_pose_udp_thread) {
        InterlockedExchange(&live_pose_udp_running, 0);
        WaitForSingleObject(live_pose_udp_thread, 1000);
        CloseHandle(live_pose_udp_thread);
    }
    if (live_pose_initialized)
        DeleteCriticalSection(&live_pose.lock);
    free(client_write);
    return 0;
}
