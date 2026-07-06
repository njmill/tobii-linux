#include <errno.h>
#include <ctype.h>
#include <libusb.h>
#include <openssl/hmac.h>
#include <openssl/evp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#define TOBII_VID 0x2104
#define TOBII_PID 0x0313
#define TOBII_VENDOR_IFACE 0
#define TOBII_EP_IN 0x83
#define TOBII_EP_OUT 0x05

#define TTP_HDR_SIZE 24
#define ENVELOPE_SIZE 8
#define TTP_MAGIC_REQ 0x51
#define TTP_MAGIC_RSP 0x52
#define TTP_MAGIC_NOTIFY 0x53
#define TTP_OP_HELLO 0x03e8
#define TTP_OP_SUBSCRIBE 0x04c4
#define TTP_OP_UNSUBSCRIBE 0x04ce
#define TTP_OP_QUERY_REALM 0x0640
#define TTP_OP_OPEN_REALM 0x076c
#define TTP_OP_REALM_RESPONSE 0x0776
#define TTP_OP_SET_DISPLAY_AREA 0x05a0
#define STREAM_GAZE 0x0500
#define STREAM_IMAGE_SECONDARY 0x0501
#define STREAM_IMAGE_COLLECTION 0x0508
#define STREAM_IMAGE_PRIMARY 0x050e
#define STREAM_SYNC 0x1771

#define DEFAULT_IMAGE_OUT "captures/ttp-mux/frames"

#define READ_BUFFER_BYTES 16384
#define ACC_BUFFER_BYTES (2 * 1024 * 1024)
#define MAX_FRAME_BYTES (2 * 1024 * 1024)
#define MAX_PACKET_BYTES 4096
#define MAX_LINE_BYTES 16384
#define DEFAULT_INIT_FILE "references/external/simonvc-tobii_ffg/captures/talon_init_out.txt"
#define REALM_KEY "IS2LJC6GIRBBEK2K\0"

static volatile sig_atomic_t stop_requested = 0;

static void handle_signal(int signal_number) {
    (void)signal_number;
    stop_requested = 1;
}

static uint64_t monotonic_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint32_t be32_at(const unsigned char *buf, size_t offset) {
    return ((uint32_t)buf[offset] << 24) |
           ((uint32_t)buf[offset + 1] << 16) |
           ((uint32_t)buf[offset + 2] << 8) |
           (uint32_t)buf[offset + 3];
}

static int32_t be_i32_at(const unsigned char *buf, size_t offset) {
    return (int32_t)be32_at(buf, offset);
}

static int64_t be_i64_at(const unsigned char *buf, size_t offset) {
    uint64_t raw = ((uint64_t)buf[offset] << 56) |
                   ((uint64_t)buf[offset + 1] << 48) |
                   ((uint64_t)buf[offset + 2] << 40) |
                   ((uint64_t)buf[offset + 3] << 32) |
                   ((uint64_t)buf[offset + 4] << 24) |
                   ((uint64_t)buf[offset + 5] << 16) |
                   ((uint64_t)buf[offset + 6] << 8) |
                   (uint64_t)buf[offset + 7];
    return (int64_t)raw;
}

static void put_be32(unsigned char *p, uint32_t v) {
    p[0] = (unsigned char)(v >> 24);
    p[1] = (unsigned char)(v >> 16);
    p[2] = (unsigned char)(v >> 8);
    p[3] = (unsigned char)v;
}

static void put_be64(unsigned char *p, uint64_t v) {
    p[0] = (unsigned char)(v >> 56);
    p[1] = (unsigned char)(v >> 48);
    p[2] = (unsigned char)(v >> 40);
    p[3] = (unsigned char)(v >> 32);
    p[4] = (unsigned char)(v >> 24);
    p[5] = (unsigned char)(v >> 16);
    p[6] = (unsigned char)(v >> 8);
    p[7] = (unsigned char)v;
}

static void put_le32(unsigned char *p, uint32_t v) {
    p[0] = (unsigned char)v;
    p[1] = (unsigned char)(v >> 8);
    p[2] = (unsigned char)(v >> 16);
    p[3] = (unsigned char)(v >> 24);
}

static double q42_at(const unsigned char *buf, size_t offset) {
    return (double)be_i64_at(buf, offset) / 4398046511104.0;
}

static double fixed16x16_at(const unsigned char *buf, size_t offset) {
    return (double)be_i32_at(buf, offset) / 65536.0;
}

static void print_libusb_error(const char *context, int err) {
    fprintf(stderr, "%s: %s (%d)\n", context, libusb_error_name(err), err);
}

static int hex_value(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int parse_hex_line(const char *line, unsigned char *out, size_t out_cap) {
    int high = -1;
    size_t len = 0;
    for (const char *p = line; *p != '\0'; p++) {
        if (*p == '#') break;
        if (isspace((unsigned char)*p)) continue;
        int nibble = hex_value((unsigned char)*p);
        if (nibble < 0) return -1;
        if (high < 0) {
            high = nibble;
        } else {
            if (len >= out_cap) return -2;
            out[len++] = (unsigned char)((high << 4) | nibble);
            high = -1;
        }
    }
    return high < 0 ? (int)len : -3;
}

static int mkdir_p_one(const char *path) {
    if (mkdir(path, 0775) == 0 || errno == EEXIST) return 0;
    perror(path);
    return -1;
}

static int ensure_default_output_dirs(void) {
    if (mkdir_p_one("captures") != 0) return -1;
    if (mkdir_p_one("captures/gaze-native") != 0) return -1;
    return 0;
}

static int make_default_csv_path(char *out, size_t out_size, const char *label) {
    time_t now = time(NULL);
    struct tm local_tm;
    if (localtime_r(&now, &local_tm) == NULL) return -1;
    int written = snprintf(out,
                           out_size,
                           "captures/gaze-native/%04d%02d%02d-%02d%02d%02d-%s.csv",
                           local_tm.tm_year + 1900,
                           local_tm.tm_mon + 1,
                           local_tm.tm_mday,
                           local_tm.tm_hour,
                           local_tm.tm_min,
                           local_tm.tm_sec,
                           label);
    return written > 0 && (size_t)written < out_size ? 0 : -1;
}

struct ttp_frame {
    uint32_t magic;
    uint32_t seq;
    uint32_t op;
    uint32_t plen;
    unsigned char payload[MAX_FRAME_BYTES];
};

struct parser {
    unsigned char acc[ACC_BUFFER_BYTES];
    size_t len;
};

struct field_view {
    uint8_t type;
    uint32_t len;
    const unsigned char *value;
};

struct image_frame {
    bool valid;
    uint32_t object_id;
    uint64_t timestamp;
    uint32_t bpp;
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    const unsigned char *image;
    uint32_t image_len;
    uint32_t payload_len;
};

struct sync_frame {
    bool valid;
    uint64_t timestamp;
    uint64_t receive_timestamp;
};

static int parser_feed(struct parser *parser, const unsigned char *data, size_t len) {
    const unsigned char *data_ptr = data;
    size_t data_len = len;

    if (parser->len >= ENVELOPE_SIZE + TTP_HDR_SIZE) {
        uint32_t plen = be32_at(parser->acc, ENVELOPE_SIZE + 20);
        size_t frame_size = ENVELOPE_SIZE + TTP_HDR_SIZE + (size_t)plen;
        if (parser->len < frame_size &&
            len >= ENVELOPE_SIZE &&
            data[0] == 0x01 &&
            data[1] == 0x00 &&
            data[2] == 0x00 &&
            data[3] == 0x00) {
            data_ptr = data + ENVELOPE_SIZE;
            data_len = len - ENVELOPE_SIZE;
        }
    }

    if (parser->len + data_len > sizeof(parser->acc)) return -1;
    memcpy(parser->acc + parser->len, data_ptr, data_len);
    parser->len += data_len;
    return 0;
}

static int parser_pop(struct parser *parser, struct ttp_frame *frame) {
    if (parser->len < ENVELOPE_SIZE) return 0;
    if (parser->acc[0] != 0x01) return -1;
    if (parser->len < ENVELOPE_SIZE + TTP_HDR_SIZE) return 0;

    uint32_t plen = be32_at(parser->acc, ENVELOPE_SIZE + 20);
    size_t frame_size = ENVELOPE_SIZE + TTP_HDR_SIZE + (size_t)plen;
    if (frame_size > MAX_FRAME_BYTES) return -1;
    if (parser->len < frame_size) return 0;

    const unsigned char *ttp = parser->acc + ENVELOPE_SIZE;
    frame->magic = be32_at(ttp, 0);
    frame->seq = be32_at(ttp, 4);
    frame->op = be32_at(ttp, 12);
    frame->plen = plen;
    if (plen > 0) memcpy(frame->payload, ttp + TTP_HDR_SIZE, plen);

    size_t remaining = parser->len - frame_size;
    if (remaining > 0) memmove(parser->acc, parser->acc + frame_size, remaining);
    parser->len = remaining;
    return 1;
}

static int read_next_frame(libusb_device_handle *handle,
                           struct parser *parser,
                           struct ttp_frame *frame,
                           int timeout_ms) {
    int popped = parser_pop(parser, frame);
    if (popped != 0) return popped;

    unsigned char buf[READ_BUFFER_BYTES];
    int transferred = 0;
    int err = libusb_bulk_transfer(handle, TOBII_EP_IN, buf, sizeof(buf), &transferred, timeout_ms);
    if (err == LIBUSB_ERROR_TIMEOUT) return 0;
    if (err != 0) return err;
    if (transferred <= 0) return 0;
    if (parser_feed(parser, buf, (size_t)transferred) != 0) return LIBUSB_ERROR_OVERFLOW;
    return parser_pop(parser, frame);
}

static size_t build_frame(unsigned char *out, uint32_t seq, uint32_t op, const unsigned char *payload, size_t plen) {
    memset(out, 0, TTP_HDR_SIZE);
    put_be32(out + 0, TTP_MAGIC_REQ);
    put_be32(out + 4, seq);
    put_be32(out + 8, 0);
    put_be32(out + 12, op);
    put_be32(out + 16, 0);
    put_be32(out + 20, (uint32_t)plen);
    if (plen > 0) memcpy(out + TTP_HDR_SIZE, payload, plen);
    return TTP_HDR_SIZE + plen;
}

static size_t wrap_envelope(unsigned char *out, const unsigned char *ttp, size_t ttp_len) {
    out[0] = 0x00;
    out[1] = 0x00;
    out[2] = 0x00;
    out[3] = 0x00;
    put_le32(out + 4, (uint32_t)ttp_len);
    memcpy(out + ENVELOPE_SIZE, ttp, ttp_len);
    return ENVELOPE_SIZE + ttp_len;
}

static size_t tlv_u32(unsigned char *out, uint32_t value) {
    out[0] = 0x02;
    put_be32(out + 1, 4);
    put_be32(out + 5, value);
    return 9;
}

static size_t tlv_tag(unsigned char *out, uint32_t value) {
    out[0] = 0x05;
    put_be32(out + 1, 4);
    put_be32(out + 5, value);
    return 9;
}

static size_t tlv_q42(unsigned char *out, double value) {
    double scaled = value * 4398046511104.0;
    int64_t rounded = scaled >= 0.0 ? (int64_t)(scaled + 0.5) : (int64_t)(scaled - 0.5);
    out[0] = 0x04;
    put_be32(out + 1, 8);
    put_be64(out + 5, (uint64_t)rounded);
    return 13;
}

static size_t tlv_point3d(unsigned char *out, double x, double y, double z) {
    size_t n = 0;
    n += tlv_tag(out + n, 0x031f41);
    n += tlv_q42(out + n, x);
    n += tlv_q42(out + n, y);
    n += tlv_q42(out + n, z);
    return n;
}

static size_t tlv_blob(unsigned char *out, const unsigned char *data, size_t len) {
    memcpy(out, data, len);
    return len;
}

static size_t build_hello(uint32_t seq, unsigned char *out) {
    static const unsigned char hello_payload[] = {
        0x00, 0x00, 0x17, 0x00, 0x00, 0x00, 0x28, 0x00, 0x00, 0x00, 0x09,
        0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x01, 0x00, 0x02,
        0x00, 0x01, 0x00, 0x03, 0x00, 0x01, 0x00, 0x04, 0x00, 0x01, 0x00, 0x05,
        0x00, 0x01, 0x00, 0x06, 0x00, 0x01, 0x00, 0x07, 0x00, 0x01, 0x00, 0x08,
    };
    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, TTP_OP_HELLO, hello_payload, sizeof(hello_payload));
    return wrap_envelope(out, ttp, ttp_len);
}

static size_t build_query_realm(uint32_t seq, unsigned char *out) {
    unsigned char payload[] = {0x00, 0x00};
    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, TTP_OP_QUERY_REALM, payload, sizeof(payload));
    return wrap_envelope(out, ttp, ttp_len);
}

static size_t build_get_display_area(uint32_t seq, unsigned char *out) {
    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, 0x0596, NULL, 0);
    return wrap_envelope(out, ttp, ttp_len);
}

static size_t build_set_display_area_corners(uint32_t seq,
                                             double tl_x,
                                             double tl_y,
                                             double tl_z,
                                             double tr_x,
                                             double tr_y,
                                             double tr_z,
                                             double bl_x,
                                             double bl_y,
                                             double bl_z,
                                             unsigned char *out) {
    unsigned char payload[256];
    size_t n = 0;
    payload[n++] = 0x00;
    payload[n++] = 0x00;
    n += tlv_point3d(payload + n, tl_x, tl_y, tl_z);
    n += tlv_point3d(payload + n, tr_x, tr_y, tr_z);
    n += tlv_point3d(payload + n, bl_x, bl_y, bl_z);
    n += tlv_tag(payload + n, 0x010100);
    n += tlv_u32(payload + n, 0x3039);

    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, TTP_OP_SET_DISPLAY_AREA, payload, n);
    return wrap_envelope(out, ttp, ttp_len);
}

static size_t build_open_realm(uint32_t seq, uint32_t realm_type, unsigned char *out) {
    unsigned char payload[64];
    size_t n = 0;
    payload[n++] = 0x00;
    payload[n++] = 0x00;
    n += tlv_u32(payload + n, realm_type);
    payload[n++] = 0x00;
    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, TTP_OP_OPEN_REALM, payload, n);
    return wrap_envelope(out, ttp, ttp_len);
}

static size_t build_realm_response(uint32_t seq,
                                   uint32_t realm_id,
                                   uint32_t field_210,
                                   const unsigned char digest[16],
                                   unsigned char *out) {
    unsigned char payload[64];
    size_t n = 0;
    payload[n++] = 0x00;
    payload[n++] = 0x00;
    n += tlv_u32(payload + n, realm_id);
    n += tlv_u32(payload + n, field_210);
    n += tlv_blob(payload + n, digest, 16);
    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, TTP_OP_REALM_RESPONSE, payload, n);
    return wrap_envelope(out, ttp, ttp_len);
}

static size_t build_subscribe(uint32_t seq, uint16_t stream_id, unsigned char *out) {
    unsigned char payload[] = {
        0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00,
        0x00, 0x17, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00,
    };
    payload[9] = (unsigned char)(stream_id >> 8);
    payload[10] = (unsigned char)stream_id;
    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, TTP_OP_SUBSCRIBE, payload, sizeof(payload));
    return wrap_envelope(out, ttp, ttp_len);
}

static size_t build_unsubscribe(uint32_t seq, uint16_t stream_id, unsigned char *out) {
    unsigned char payload[] = {
        0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00,
        0x00, 0x17, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00,
    };
    payload[9] = (unsigned char)(stream_id >> 8);
    payload[10] = (unsigned char)stream_id;
    unsigned char ttp[MAX_PACKET_BYTES];
    size_t ttp_len = build_frame(ttp, seq, TTP_OP_UNSUBSCRIBE, payload, sizeof(payload));
    return wrap_envelope(out, ttp, ttp_len);
}

static int send_packet(libusb_device_handle *handle, const unsigned char *packet, size_t len) {
    int transferred = 0;
    int err = libusb_bulk_transfer(handle, TOBII_EP_OUT, (unsigned char *)packet, (int)len, &transferred, 1000);
    if (err != 0) return err;
    return transferred == (int)len ? 0 : LIBUSB_ERROR_IO;
}

static int send_subscribe(libusb_device_handle *handle, uint32_t seq, uint16_t stream_id, const char *reason) {
    unsigned char packet[MAX_PACKET_BYTES];
    size_t packet_len = build_subscribe(seq, stream_id, packet);
    int err = send_packet(handle, packet, packet_len);
    if (err != 0) {
        fprintf(stderr,
                "subscribe reason=%s stream=0x%04x seq=%u error=%s (%d)\n",
                reason,
                stream_id,
                seq,
                libusb_error_name(err),
                err);
        return err;
    }
    fprintf(stderr,
            "subscribe reason=%s stream=0x%04x seq=%u bytes=%zu\n",
            reason,
            stream_id,
            seq,
            packet_len);
    return 0;
}

static int send_unsubscribe(libusb_device_handle *handle, uint32_t seq, uint16_t stream_id, const char *reason) {
    unsigned char packet[MAX_PACKET_BYTES];
    size_t packet_len = build_unsubscribe(seq, stream_id, packet);
    int err = send_packet(handle, packet, packet_len);
    if (err != 0) {
        fprintf(stderr,
                "unsubscribe reason=%s stream=0x%04x seq=%u error=%s (%d)\n",
                reason,
                stream_id,
                seq,
                libusb_error_name(err),
                err);
        return err;
    }
    fprintf(stderr,
            "unsubscribe reason=%s stream=0x%04x seq=%u bytes=%zu\n",
            reason,
            stream_id,
            seq,
            packet_len);
    return 0;
}

static int drain_available_frames(libusb_device_handle *handle, struct parser *parser, int max_reads) {
    struct ttp_frame frame;
    int drained = 0;
    for (int i = 0; i < max_reads; i++) {
        int rc = read_next_frame(handle, parser, &frame, 30);
        if (rc == 0) break;
        if (rc < 0) return rc;
        drained++;
    }
    return drained;
}

static int send_public_init_sequence(libusb_device_handle *handle,
                                     struct parser *parser,
                                     const char *path,
                                     int *command_count) {
    FILE *file = fopen(path, "r");
    if (file == NULL) {
        perror(path);
        return -1;
    }

    char line[MAX_LINE_BYTES];
    unsigned char command[MAX_PACKET_BYTES];
    int count = 0;

    while (fgets(line, sizeof(line), file) != NULL) {
        int command_len = parse_hex_line(line, command, sizeof(command));
        if (command_len == 0) continue;
        if (command_len < 0) {
            fprintf(stderr, "public_init parse_error command=%d code=%d\n", count, command_len);
            fclose(file);
            return -1;
        }
        int err = send_packet(handle, command, (size_t)command_len);
        if (err != 0) {
            fprintf(stderr, "public_init command=%d error=%s (%d)\n", count, libusb_error_name(err), err);
            fclose(file);
            return err;
        }
        int drained = drain_available_frames(handle, parser, 4);
        if (drained < 0) {
            fprintf(stderr, "public_init drain command=%d error=%s (%d)\n", count, libusb_error_name(drained), drained);
            fclose(file);
            return drained;
        }
        count++;
    }

    fclose(file);
    *command_count = count;
    return 0;
}

static int send_request_wait_response(libusb_device_handle *handle,
                                      struct parser *parser,
                                      const char *name,
                                      const unsigned char *packet,
                                      size_t packet_len,
                                      uint32_t seq,
                                      struct ttp_frame *response) {
    int err = send_packet(handle, packet, packet_len);
    if (err != 0) {
        fprintf(stderr, "handshake_send name=%s error=%s (%d)\n", name, libusb_error_name(err), err);
        return err;
    }

    uint64_t deadline = monotonic_ns() + 2000000000ull;
    while (!stop_requested && monotonic_ns() < deadline) {
        int rc = read_next_frame(handle, parser, response, 100);
        if (rc == 0) continue;
        if (rc < 0) return rc;
        if (response->magic == TTP_MAGIC_RSP && response->seq == seq) {
            fprintf(stderr,
                    "handshake_response name=%s seq=%u op=0x%04x payload_len=%u\n",
                    name,
                    response->seq,
                    response->op,
                    response->plen);
            return 0;
        }
    }

    fprintf(stderr, "handshake_response name=%s timeout seq=%u\n", name, seq);
    return LIBUSB_ERROR_TIMEOUT;
}

static int send_display_area_override(libusb_device_handle *handle,
                                      struct parser *parser,
                                      const char *mode,
                                      uint32_t seq,
                                      double width_mm,
                                      double height_mm,
                                      double origin_x_mm,
                                      double origin_y_mm,
                                      double z_mm) {
    if (strcmp(mode, "none") == 0) return 0;

    double tl_x = origin_x_mm;
    double tl_y = origin_y_mm + height_mm;
    double tl_z = z_mm;
    double tr_x = origin_x_mm + width_mm;
    double tr_y = origin_y_mm + height_mm;
    double tr_z = z_mm;
    double bl_x = origin_x_mm;
    double bl_y = origin_y_mm;
    double bl_z = z_mm;

    if (strcmp(mode, "big") == 0) {
        tl_x = -500.0;
        tl_y = 500.0;
        tl_z = 0.0;
        tr_x = 500.0;
        tr_y = 500.0;
        tr_z = 0.0;
        bl_x = -500.0;
        bl_y = 0.0;
        bl_z = 0.0;
    } else if (strcmp(mode, "rect") != 0) {
        fprintf(stderr, "unknown display-area mode: %s\n", mode);
        return LIBUSB_ERROR_INVALID_PARAM;
    }

    unsigned char packet[MAX_PACKET_BYTES];
    size_t packet_len = build_set_display_area_corners(seq, tl_x, tl_y, tl_z, tr_x, tr_y, tr_z, bl_x, bl_y, bl_z, packet);
    int err = send_packet(handle, packet, packet_len);
    if (err != 0) {
        fprintf(stderr, "display_area_override=%s error=%s (%d)\n", mode, libusb_error_name(err), err);
        return err;
    }
    int drained = drain_available_frames(handle, parser, 8);
    if (drained < 0) {
        fprintf(stderr, "display_area_override drain error=%s (%d)\n", libusb_error_name(drained), drained);
        return drained;
    }
    fprintf(stderr,
            "display_area_override=%s seq=%u tl=(%.1f,%.1f,%.1f) tr=(%.1f,%.1f,%.1f) bl=(%.1f,%.1f,%.1f) drained=%d\n",
            mode,
            seq,
            tl_x,
            tl_y,
            tl_z,
            tr_x,
            tr_y,
            tr_z,
            bl_x,
            bl_y,
            bl_z,
            drained);
    return 0;
}

struct tlv_reader {
    const unsigned char *buf;
    size_t len;
    size_t pos;
};

static bool tlv_read_header(struct tlv_reader *reader, uint8_t *type, uint32_t *size) {
    if (reader->len - reader->pos < 5) return false;
    *type = reader->buf[reader->pos];
    *size = be32_at(reader->buf, reader->pos + 1);
    reader->pos += 5;
    return reader->len - reader->pos >= *size;
}

static bool tlv_read_tag(struct tlv_reader *reader, uint32_t *tag) {
    uint8_t type = 0;
    uint32_t size = 0;
    if (!tlv_read_header(reader, &type, &size) || type != 5 || size != 4) return false;
    *tag = be32_at(reader->buf, reader->pos);
    reader->pos += 4;
    return true;
}

static bool tlv_read_u32(struct tlv_reader *reader, uint32_t *value) {
    uint8_t type = 0;
    uint32_t size = 0;
    if (!tlv_read_header(reader, &type, &size) || type != 2 || size != 4) return false;
    *value = be32_at(reader->buf, reader->pos);
    reader->pos += 4;
    return true;
}

static bool tlv_read_s64_as_double(struct tlv_reader *reader, double *value) {
    uint8_t type = 0;
    uint32_t size = 0;
    if (!tlv_read_header(reader, &type, &size) || type != 6 || size != 8) return false;
    *value = (double)be_i64_at(reader->buf, reader->pos);
    reader->pos += 8;
    return true;
}

static bool tlv_read_fixed16x16(struct tlv_reader *reader, double *value) {
    uint8_t type = 0;
    uint32_t size = 0;
    if (!tlv_read_header(reader, &type, &size) || type != 3 || size != 4) return false;
    *value = fixed16x16_at(reader->buf, reader->pos);
    reader->pos += 4;
    return true;
}

static bool tlv_read_q42(struct tlv_reader *reader, double *value) {
    uint8_t type = 0;
    uint32_t size = 0;
    if (!tlv_read_header(reader, &type, &size) || type != 4 || size != 8) return false;
    *value = q42_at(reader->buf, reader->pos);
    reader->pos += 8;
    return true;
}

static bool tlv_read_xds_row(struct tlv_reader *reader, uint32_t *count) {
    uint32_t tag = 0;
    if (!tlv_read_tag(reader, &tag) || (tag & 0xffffu) != 0x0bb8u) return false;
    *count = (tag >> 16) & 0xfffu;
    return true;
}

static bool tlv_read_xds_column(struct tlv_reader *reader, uint32_t *column) {
    uint32_t tag = 0;
    if (!tlv_read_tag(reader, &tag) || tag != 0x020bb9u) return false;
    return tlv_read_u32(reader, column);
}

static bool tlv_read_point2d(struct tlv_reader *reader, double *x, double *y) {
    uint32_t tag = 0;
    if (!tlv_read_tag(reader, &tag) || tag != 0x021f40u) return false;
    return tlv_read_q42(reader, x) && tlv_read_q42(reader, y);
}

static bool tlv_read_point3d(struct tlv_reader *reader, double *x, double *y, double *z) {
    uint32_t tag = 0;
    if (!tlv_read_tag(reader, &tag) || tag != 0x031f41u) return false;
    return tlv_read_q42(reader, x) && tlv_read_q42(reader, y) && tlv_read_q42(reader, z);
}

static bool tlv_skip_known(struct tlv_reader *reader, uint32_t column) {
    double x = 0.0, y = 0.0, z = 0.0;
    uint32_t u = 0;
    switch (column) {
        case 0x01:
            return tlv_read_s64_as_double(reader, &x);
        case 0x02:
        case 0x03:
        case 0x04:
        case 0x08:
        case 0x09:
        case 0x0a:
        case 0x17:
        case 0x18:
        case 0x22:
        case 0x24:
        case 0x25:
        case 0x27:
            return tlv_read_point3d(reader, &x, &y, &z);
        case 0x05:
        case 0x0b:
        case 0x19:
        case 0x1a:
        case 0x1c:
        case 0x20:
            return tlv_read_point2d(reader, &x, &y);
        case 0x06:
        case 0x0c:
        case 0x29:
        case 0x2b:
            return tlv_read_fixed16x16(reader, &x);
        case 0x07:
        case 0x0d:
        case 0x0e:
        case 0x11:
        case 0x14:
        case 0x15:
        case 0x16:
        case 0x1b:
        case 0x1d:
        case 0x1e:
        case 0x1f:
        case 0x21:
        case 0x23:
        case 0x26:
        case 0x28:
        case 0x2a:
        case 0x2c:
            return tlv_read_u32(reader, &u);
        default:
            return false;
    }
}

static uint32_t tlv_first_u32(const unsigned char *payload, uint32_t payload_len) {
    struct tlv_reader reader = {.buf = payload, .len = payload_len, .pos = payload_len >= 2 ? 2 : 0};
    uint32_t value = 0;
    return tlv_read_u32(&reader, &value) ? value : 0;
}

static uint32_t tlv_u32_at_index(const unsigned char *payload, uint32_t payload_len, int wanted_index) {
    struct tlv_reader reader = {.buf = payload, .len = payload_len, .pos = payload_len >= 2 ? 2 : 0};
    for (int i = 0; i <= wanted_index; i++) {
        uint32_t value = 0;
        if (!tlv_read_u32(&reader, &value)) return 0;
        if (i == wanted_index) return value;
    }
    return 0;
}

static bool tlv_find_challenge(const unsigned char *payload,
                               uint32_t payload_len,
                               const unsigned char **challenge,
                               size_t *challenge_len) {
    struct tlv_reader reader = {.buf = payload, .len = payload_len, .pos = payload_len >= 2 ? 2 : 0};
    for (int i = 0; i < 2; i++) {
        uint32_t ignored = 0;
        if (!tlv_read_u32(&reader, &ignored)) return false;
    }
    if (reader.pos >= reader.len) return false;
    *challenge = reader.buf + reader.pos;
    *challenge_len = reader.len - reader.pos;
    return *challenge_len > 0;
}

struct gaze_sample {
    bool has_timestamp;
    bool has_frame_counter;
    bool has_gaze_2d;
    bool has_gaze_unfiltered;
    bool has_left_2d;
    bool has_right_2d;
    bool has_left_origin;
    bool has_right_origin;
    bool has_left_raw_origin;
    bool has_right_raw_origin;
    bool has_left_trackbox;
    bool has_right_trackbox;
    bool has_left_display_origin;
    bool has_right_display_origin;
    bool has_left_trackbox_display;
    bool has_right_trackbox_display;
    bool has_pupil_l;
    bool has_pupil_r;
    int64_t timestamp_us;
    uint32_t frame_counter;
    uint32_t validity_l;
    uint32_t validity_r;
    uint32_t eye_present_l;
    uint32_t eye_present_r;
    uint32_t binocular_flag;
    uint32_t gaze_valid;
    uint32_t gaze_l_valid;
    uint32_t gaze_r_valid;
    uint32_t gaze_unfiltered_valid;
    double gaze_x;
    double gaze_y;
    double gaze_unfiltered_x;
    double gaze_unfiltered_y;
    double gaze_l_x;
    double gaze_l_y;
    double gaze_r_x;
    double gaze_r_y;
    double eye_l_x;
    double eye_l_y;
    double eye_l_z;
    double eye_r_x;
    double eye_r_y;
    double eye_r_z;
    double raw_eye_l_x;
    double raw_eye_l_y;
    double raw_eye_l_z;
    double raw_eye_r_x;
    double raw_eye_r_y;
    double raw_eye_r_z;
    double trackbox_l_x;
    double trackbox_l_y;
    double trackbox_l_z;
    double trackbox_r_x;
    double trackbox_r_y;
    double trackbox_r_z;
    double eye_l_display_x;
    double eye_l_display_y;
    double eye_l_display_z;
    double eye_r_display_x;
    double eye_r_display_y;
    double eye_r_display_z;
    double trackbox_l_display_x;
    double trackbox_l_display_y;
    double trackbox_l_display_z;
    double trackbox_r_display_x;
    double trackbox_r_display_y;
    double trackbox_r_display_z;
    double pupil_l_mm;
    double pupil_r_mm;
};

static bool decode_gaze_sample(const unsigned char *payload, uint32_t payload_len, struct gaze_sample *sample) {
    memset(sample, 0, sizeof(*sample));
    sample->validity_l = UINT32_MAX;
    sample->validity_r = UINT32_MAX;
    sample->eye_present_l = UINT32_MAX;
    sample->eye_present_r = UINT32_MAX;
    sample->binocular_flag = UINT32_MAX;
    sample->gaze_valid = UINT32_MAX;
    sample->gaze_l_valid = UINT32_MAX;
    sample->gaze_r_valid = UINT32_MAX;
    sample->gaze_unfiltered_valid = UINT32_MAX;

    if (payload_len < 2) return false;
    struct tlv_reader reader = {.buf = payload, .len = payload_len, .pos = 2};
    uint32_t n_cols = 0;
    if (!tlv_read_xds_row(&reader, &n_cols)) return false;

    for (uint32_t i = 0; i < n_cols && reader.pos < reader.len; i++) {
        uint32_t col = 0;
        if (!tlv_read_xds_column(&reader, &col)) return true;
        switch (col) {
            case 0x01: {
                double v = 0.0;
                if (!tlv_read_s64_as_double(&reader, &v)) return true;
                sample->timestamp_us = (int64_t)v;
                sample->has_timestamp = true;
                break;
            }
            case 0x02:
                sample->has_left_origin = tlv_read_point3d(&reader, &sample->eye_l_x, &sample->eye_l_y, &sample->eye_l_z);
                if (!sample->has_left_origin) return true;
                break;
            case 0x03:
                sample->has_left_trackbox = tlv_read_point3d(&reader, &sample->trackbox_l_x, &sample->trackbox_l_y, &sample->trackbox_l_z);
                if (!sample->has_left_trackbox) return true;
                break;
            case 0x05:
                sample->has_left_2d = tlv_read_point2d(&reader, &sample->gaze_l_x, &sample->gaze_l_y);
                if (!sample->has_left_2d) return true;
                break;
            case 0x06:
                sample->has_pupil_l = tlv_read_fixed16x16(&reader, &sample->pupil_l_mm);
                if (!sample->has_pupil_l) return true;
                break;
            case 0x07:
                if (!tlv_read_u32(&reader, &sample->validity_l)) return true;
                break;
            case 0x08:
                sample->has_right_origin = tlv_read_point3d(&reader, &sample->eye_r_x, &sample->eye_r_y, &sample->eye_r_z);
                if (!sample->has_right_origin) return true;
                break;
            case 0x09:
                sample->has_right_trackbox = tlv_read_point3d(&reader, &sample->trackbox_r_x, &sample->trackbox_r_y, &sample->trackbox_r_z);
                if (!sample->has_right_trackbox) return true;
                break;
            case 0x0b:
                sample->has_right_2d = tlv_read_point2d(&reader, &sample->gaze_r_x, &sample->gaze_r_y);
                if (!sample->has_right_2d) return true;
                break;
            case 0x0c:
                sample->has_pupil_r = tlv_read_fixed16x16(&reader, &sample->pupil_r_mm);
                if (!sample->has_pupil_r) return true;
                break;
            case 0x0d:
                if (!tlv_read_u32(&reader, &sample->validity_r)) return true;
                break;
            case 0x14:
                if (!tlv_read_u32(&reader, &sample->frame_counter)) return true;
                sample->has_frame_counter = true;
                break;
            case 0x15:
                if (!tlv_read_u32(&reader, &sample->eye_present_l)) return true;
                break;
            case 0x16:
                if (!tlv_read_u32(&reader, &sample->eye_present_r)) return true;
                break;
            case 0x17:
                sample->has_left_raw_origin = tlv_read_point3d(&reader, &sample->raw_eye_l_x, &sample->raw_eye_l_y, &sample->raw_eye_l_z);
                if (!sample->has_left_raw_origin) return true;
                break;
            case 0x18:
                sample->has_right_raw_origin = tlv_read_point3d(&reader, &sample->raw_eye_r_x, &sample->raw_eye_r_y, &sample->raw_eye_r_z);
                if (!sample->has_right_raw_origin) return true;
                break;
            case 0x1b:
                if (!tlv_read_u32(&reader, &sample->binocular_flag)) return true;
                break;
            case 0x1c:
                sample->has_gaze_2d = tlv_read_point2d(&reader, &sample->gaze_x, &sample->gaze_y);
                if (!sample->has_gaze_2d) return true;
                break;
            case 0x1d:
                if (!tlv_read_u32(&reader, &sample->gaze_valid)) return true;
                break;
            case 0x1e:
                if (!tlv_read_u32(&reader, &sample->gaze_l_valid)) return true;
                break;
            case 0x1f:
                if (!tlv_read_u32(&reader, &sample->gaze_r_valid)) return true;
                break;
            case 0x20:
                sample->has_gaze_unfiltered = tlv_read_point2d(&reader, &sample->gaze_unfiltered_x, &sample->gaze_unfiltered_y);
                if (!sample->has_gaze_unfiltered) return true;
                break;
            case 0x21:
                if (!tlv_read_u32(&reader, &sample->gaze_unfiltered_valid)) return true;
                break;
            case 0x22:
                sample->has_left_display_origin = tlv_read_point3d(&reader, &sample->eye_l_display_x, &sample->eye_l_display_y, &sample->eye_l_display_z);
                if (!sample->has_left_display_origin) return true;
                break;
            case 0x24:
                sample->has_right_display_origin = tlv_read_point3d(&reader, &sample->eye_r_display_x, &sample->eye_r_display_y, &sample->eye_r_display_z);
                if (!sample->has_right_display_origin) return true;
                break;
            case 0x25:
                sample->has_left_trackbox_display = tlv_read_point3d(&reader, &sample->trackbox_l_display_x, &sample->trackbox_l_display_y, &sample->trackbox_l_display_z);
                if (!sample->has_left_trackbox_display) return true;
                break;
            case 0x27:
                sample->has_right_trackbox_display = tlv_read_point3d(&reader, &sample->trackbox_r_display_x, &sample->trackbox_r_display_y, &sample->trackbox_r_display_z);
                if (!sample->has_right_trackbox_display) return true;
                break;
            default:
                if (!tlv_skip_known(&reader, col)) return true;
                break;
        }
    }

    return true;
}

static int run_tobiifree_handshake(libusb_device_handle *handle, struct parser *parser, uint32_t *next_seq) {
    unsigned char packet[MAX_PACKET_BYTES];
    struct ttp_frame response;
    uint32_t seq = 1;

    size_t packet_len = build_hello(seq, packet);
    int err = send_request_wait_response(handle, parser, "hello", packet, packet_len, seq, &response);
    if (err != 0) return err;
    seq++;

    packet_len = build_query_realm(seq, packet);
    err = send_request_wait_response(handle, parser, "query_realm", packet, packet_len, seq, &response);
    if (err != 0) return err;
    uint32_t realm_type = tlv_first_u32(response.payload, response.plen);
    fprintf(stderr, "realm_type=%u\n", realm_type);
    seq++;

    packet_len = build_open_realm(seq, realm_type, packet);
    err = send_request_wait_response(handle, parser, "open_realm", packet, packet_len, seq, &response);
    if (err != 0) return err;
    seq++;

    if (realm_type != 0) {
        uint32_t realm_id = tlv_u32_at_index(response.payload, response.plen, 0);
        uint32_t field_210 = tlv_u32_at_index(response.payload, response.plen, 1);
        const unsigned char *challenge = NULL;
        size_t challenge_len = 0;
        if (!tlv_find_challenge(response.payload, response.plen, &challenge, &challenge_len)) {
            fprintf(stderr, "realm_auth=parse_challenge_failed\n");
            return LIBUSB_ERROR_OTHER;
        }

        unsigned char digest[EVP_MAX_MD_SIZE];
        unsigned int digest_len = 0;
        HMAC(EVP_md5(),
             REALM_KEY,
             (int)sizeof(REALM_KEY) - 1,
             challenge,
             challenge_len,
             digest,
             &digest_len);
        if (digest_len < 16) return LIBUSB_ERROR_OTHER;

        packet_len = build_realm_response(seq, realm_id, field_210, digest, packet);
        err = send_request_wait_response(handle, parser, "realm_response", packet, packet_len, seq, &response);
        if (err != 0) return err;
        seq++;
    }

    err = send_subscribe(handle, seq, STREAM_GAZE, "startup");
    if (err != 0) return err;
    *next_seq = seq + 1;
    return 0;
}

static int run_tobiifree_post_connect(libusb_device_handle *handle, struct parser *parser, uint32_t *next_seq) {
    unsigned char packet[MAX_PACKET_BYTES];
    struct ttp_frame response;
    uint32_t seq = *next_seq;

    size_t packet_len = build_get_display_area(seq, packet);
    int err = send_request_wait_response(handle, parser, "get_display_area", packet, packet_len, seq, &response);
    if (err != 0) {
        fprintf(stderr, "post_connect get_display_area=warning error=%s (%d)\n", libusb_error_name(err), err);
    } else {
        fprintf(stderr, "post_connect get_display_area=ok payload_len=%u\n", response.plen);
    }
    seq++;

    err = send_subscribe(handle, seq, STREAM_GAZE, "post_connect");
    if (err != 0) return err;
    *next_seq = seq + 1;
    return 0;
}

static void write_csv_header(FILE *csv) {
    fprintf(csv,
            "label,elapsed_ms,monotonic_ns,sample_index,payload_len,seq,frame_counter,timestamp_us,"
            "gaze_valid,gaze_x_norm,gaze_y_norm,gaze_unfiltered_valid,gaze_unfiltered_x_norm,gaze_unfiltered_y_norm,"
            "validity_l,validity_r,eye_present_l,eye_present_r,binocular_flag,"
            "gaze_l_valid,gaze_l_x_norm,gaze_l_y_norm,gaze_r_valid,gaze_r_x_norm,gaze_r_y_norm,"
            "pupil_l_mm,pupil_r_mm,"
            "eye_origin_l_x_mm,eye_origin_l_y_mm,eye_origin_l_z_mm,"
            "eye_origin_r_x_mm,eye_origin_r_y_mm,eye_origin_r_z_mm,"
            "raw_eye_origin_l_x_mm,raw_eye_origin_l_y_mm,raw_eye_origin_l_z_mm,"
            "raw_eye_origin_r_x_mm,raw_eye_origin_r_y_mm,raw_eye_origin_r_z_mm,"
            "trackbox_l_x,trackbox_l_y,trackbox_l_z,trackbox_r_x,trackbox_r_y,trackbox_r_z,"
            "eye_origin_l_display_x_mm,eye_origin_l_display_y_mm,eye_origin_l_display_z_mm,"
            "eye_origin_r_display_x_mm,eye_origin_r_display_y_mm,eye_origin_r_display_z_mm,"
            "trackbox_l_display_x,trackbox_l_display_y,trackbox_l_display_z,"
            "trackbox_r_display_x,trackbox_r_display_y,trackbox_r_display_z\n");
}

static uint32_t csv_u32_or_empty(FILE *csv, uint32_t value) {
    if (value == UINT32_MAX) {
        return (uint32_t)fprintf(csv, ",");
    }
    return (uint32_t)fprintf(csv, "%u,", value);
}

static void csv_double_or_empty(FILE *csv, bool present, double value, bool last) {
    if (present) {
        fprintf(csv, "%.9f%s", value, last ? "" : ",");
    } else {
        fprintf(csv, "%s", last ? "" : ",");
    }
}

static void write_csv_sample(FILE *csv,
                             const char *label,
                             uint64_t start_ns,
                             uint64_t now_ns,
                             int sample_index,
                             const struct ttp_frame *frame,
                             const struct gaze_sample *sample) {
    fprintf(csv,
            "%s,%.3f,%llu,%d,%u,%u,",
            label,
            (double)(now_ns - start_ns) / 1000000.0,
            (unsigned long long)now_ns,
            sample_index,
            frame->plen,
            frame->seq);

    if (sample->has_frame_counter) fprintf(csv, "%u,", sample->frame_counter);
    else fprintf(csv, ",");
    if (sample->has_timestamp) fprintf(csv, "%lld,", (long long)sample->timestamp_us);
    else fprintf(csv, ",");

    csv_u32_or_empty(csv, sample->gaze_valid);
    csv_double_or_empty(csv, sample->has_gaze_2d, sample->gaze_x, false);
    csv_double_or_empty(csv, sample->has_gaze_2d, sample->gaze_y, false);
    csv_u32_or_empty(csv, sample->gaze_unfiltered_valid);
    csv_double_or_empty(csv, sample->has_gaze_unfiltered, sample->gaze_unfiltered_x, false);
    csv_double_or_empty(csv, sample->has_gaze_unfiltered, sample->gaze_unfiltered_y, false);
    csv_u32_or_empty(csv, sample->validity_l);
    csv_u32_or_empty(csv, sample->validity_r);
    csv_u32_or_empty(csv, sample->eye_present_l);
    csv_u32_or_empty(csv, sample->eye_present_r);
    csv_u32_or_empty(csv, sample->binocular_flag);
    csv_u32_or_empty(csv, sample->gaze_l_valid);
    csv_double_or_empty(csv, sample->has_left_2d, sample->gaze_l_x, false);
    csv_double_or_empty(csv, sample->has_left_2d, sample->gaze_l_y, false);
    csv_u32_or_empty(csv, sample->gaze_r_valid);
    csv_double_or_empty(csv, sample->has_right_2d, sample->gaze_r_x, false);
    csv_double_or_empty(csv, sample->has_right_2d, sample->gaze_r_y, false);
    csv_double_or_empty(csv, sample->has_pupil_l, sample->pupil_l_mm, false);
    csv_double_or_empty(csv, sample->has_pupil_r, sample->pupil_r_mm, false);
    csv_double_or_empty(csv, sample->has_left_origin, sample->eye_l_x, false);
    csv_double_or_empty(csv, sample->has_left_origin, sample->eye_l_y, false);
    csv_double_or_empty(csv, sample->has_left_origin, sample->eye_l_z, false);
    csv_double_or_empty(csv, sample->has_right_origin, sample->eye_r_x, false);
    csv_double_or_empty(csv, sample->has_right_origin, sample->eye_r_y, false);
    csv_double_or_empty(csv, sample->has_right_origin, sample->eye_r_z, false);
    csv_double_or_empty(csv, sample->has_left_raw_origin, sample->raw_eye_l_x, false);
    csv_double_or_empty(csv, sample->has_left_raw_origin, sample->raw_eye_l_y, false);
    csv_double_or_empty(csv, sample->has_left_raw_origin, sample->raw_eye_l_z, false);
    csv_double_or_empty(csv, sample->has_right_raw_origin, sample->raw_eye_r_x, false);
    csv_double_or_empty(csv, sample->has_right_raw_origin, sample->raw_eye_r_y, false);
    csv_double_or_empty(csv, sample->has_right_raw_origin, sample->raw_eye_r_z, false);
    csv_double_or_empty(csv, sample->has_left_trackbox, sample->trackbox_l_x, false);
    csv_double_or_empty(csv, sample->has_left_trackbox, sample->trackbox_l_y, false);
    csv_double_or_empty(csv, sample->has_left_trackbox, sample->trackbox_l_z, false);
    csv_double_or_empty(csv, sample->has_right_trackbox, sample->trackbox_r_x, false);
    csv_double_or_empty(csv, sample->has_right_trackbox, sample->trackbox_r_y, false);
    csv_double_or_empty(csv, sample->has_right_trackbox, sample->trackbox_r_z, false);
    csv_double_or_empty(csv, sample->has_left_display_origin, sample->eye_l_display_x, false);
    csv_double_or_empty(csv, sample->has_left_display_origin, sample->eye_l_display_y, false);
    csv_double_or_empty(csv, sample->has_left_display_origin, sample->eye_l_display_z, false);
    csv_double_or_empty(csv, sample->has_right_display_origin, sample->eye_r_display_x, false);
    csv_double_or_empty(csv, sample->has_right_display_origin, sample->eye_r_display_y, false);
    csv_double_or_empty(csv, sample->has_right_display_origin, sample->eye_r_display_z, false);
    csv_double_or_empty(csv, sample->has_left_trackbox_display, sample->trackbox_l_display_x, false);
    csv_double_or_empty(csv, sample->has_left_trackbox_display, sample->trackbox_l_display_y, false);
    csv_double_or_empty(csv, sample->has_left_trackbox_display, sample->trackbox_l_display_z, false);
    csv_double_or_empty(csv, sample->has_right_trackbox_display, sample->trackbox_r_display_x, false);
    csv_double_or_empty(csv, sample->has_right_trackbox_display, sample->trackbox_r_display_y, false);
    csv_double_or_empty(csv, sample->has_right_trackbox_display, sample->trackbox_r_display_z, true);
    fprintf(csv, "\n");
}

static int mkdir_p_recursive(const char *path) {
    char tmp[1024];
    size_t len = strlen(path);
    if (len == 0 || len >= sizeof(tmp)) return -1;
    memcpy(tmp, path, len + 1);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(tmp, 0775) != 0 && errno != EEXIST) return -1;
            *p = '/';
        }
    }
    if (mkdir(tmp, 0775) != 0 && errno != EEXIST) return -1;
    return 0;
}

static bool next_field(const unsigned char *payload,
                       uint32_t payload_len,
                       uint32_t *offset,
                       struct field_view *field) {
    if (*offset + 5 > payload_len) return false;
    field->type = payload[*offset];
    field->len = be32_at(payload, *offset + 1);
    if (field->len > payload_len - *offset - 5) return false;
    field->value = payload + *offset + 5;
    *offset += 5 + field->len;
    return true;
}

static bool decode_image_payload(uint32_t object_id,
                                 const unsigned char *payload,
                                 uint32_t payload_len,
                                 struct image_frame *frame) {
    memset(frame, 0, sizeof(*frame));
    frame->object_id = object_id;
    frame->payload_len = payload_len;
    if (payload_len < 2) return false;

    uint32_t offset = 2;
    const unsigned char *image_value = NULL;
    uint32_t image_value_len = 0;
    while (offset + 5 <= payload_len) {
        struct field_view marker;
        struct field_view id_field;
        struct field_view value_field;
        if (!next_field(payload, payload_len, &offset, &marker) || marker.type != 0x05) return false;
        if (!next_field(payload, payload_len, &offset, &id_field)) return false;
        if (id_field.type == 0x05) {
            marker = id_field;
            if (!next_field(payload, payload_len, &offset, &id_field)) return false;
        }
        (void)marker;
        if (id_field.type != 0x02 || id_field.len != 4) return false;
        if (!next_field(payload, payload_len, &offset, &value_field)) return false;

        uint32_t column_id = be32_at(id_field.value, 0);
        if (column_id == 0x0001 && value_field.len == 8) {
            frame->timestamp = ((uint64_t)be32_at(value_field.value, 0) << 32) | be32_at(value_field.value, 4);
        } else if (column_id == 0x0002 && value_field.len == 4) {
            frame->bpp = be32_at(value_field.value, 0);
        } else if (column_id == 0x0003 && value_field.len == 4) {
            frame->width = be32_at(value_field.value, 0);
        } else if (object_id == STREAM_IMAGE_PRIMARY && column_id == 0x0004 && value_field.len == 4) {
            frame->stride = be32_at(value_field.value, 0);
        } else if (object_id == STREAM_IMAGE_PRIMARY && column_id == 0x0005 && value_field.len == 4) {
            frame->height = be32_at(value_field.value, 0);
        } else if (object_id != STREAM_IMAGE_PRIMARY && column_id == 0x0004 && value_field.len == 4) {
            frame->height = be32_at(value_field.value, 0);
        } else if (object_id == STREAM_IMAGE_PRIMARY && column_id == 0x0006) {
            image_value = value_field.value;
            image_value_len = value_field.len;
        } else if (object_id != STREAM_IMAGE_PRIMARY && column_id == 0x0005) {
            image_value = value_field.value;
            image_value_len = value_field.len;
        }
    }

    if (frame->stride == 0) frame->stride = frame->width;
    if (image_value == NULL || image_value_len < 4) return false;
    uint32_t image_len = be32_at(image_value, 0);
    if (image_len > image_value_len - 4 ||
        frame->width == 0 ||
        frame->height == 0 ||
        frame->stride == 0 ||
        frame->width > frame->stride ||
        image_len < frame->stride * frame->height) {
        return false;
    }
    frame->image = image_value + 4;
    frame->image_len = image_len;
    frame->valid = true;
    return true;
}

static bool decode_sync_payload(const unsigned char *payload,
                                uint32_t payload_len,
                                struct sync_frame *frame) {
    memset(frame, 0, sizeof(*frame));
    if (payload_len < 2) return false;

    uint32_t offset = 2;
    while (offset + 5 <= payload_len) {
        struct field_view marker;
        struct field_view id_field;
        struct field_view value_field;
        if (!next_field(payload, payload_len, &offset, &marker) || marker.type != 0x05) return false;
        if (!next_field(payload, payload_len, &offset, &id_field)) return false;
        if (id_field.type == 0x05) {
            marker = id_field;
            if (!next_field(payload, payload_len, &offset, &id_field)) return false;
        }
        (void)marker;
        if (id_field.type != 0x02 || id_field.len != 4) return false;
        if (!next_field(payload, payload_len, &offset, &value_field)) return false;
        uint32_t column_id = be32_at(id_field.value, 0);
        if (column_id == 1 && value_field.len == 8) {
            frame->timestamp = ((uint64_t)be32_at(value_field.value, 0) << 32) | be32_at(value_field.value, 4);
        } else if (column_id == 2 && value_field.len == 8) {
            frame->receive_timestamp = ((uint64_t)be32_at(value_field.value, 0) << 32) | be32_at(value_field.value, 4);
        }
    }
    frame->valid = frame->timestamp != 0 || frame->receive_timestamp != 0;
    return frame->valid;
}

static uint32_t stream_kind_alias(uint32_t object_id) {
    if (object_id == STREAM_IMAGE_SECONDARY) return 0x0001;
    if (object_id == STREAM_IMAGE_COLLECTION) return 0x0003;
    if (object_id == STREAM_IMAGE_PRIMARY) return 0x0002;
    return object_id & 0xffffu;
}

static bool is_image_stream(uint32_t object_id) {
    return object_id == STREAM_IMAGE_SECONDARY ||
           object_id == STREAM_IMAGE_COLLECTION ||
           object_id == STREAM_IMAGE_PRIMARY;
}

static int write_pgm_frame(const char *out_dir,
                           const struct image_frame *frame,
                           int index,
                           int slot,
                           bool use_ring,
                           char *path,
                           size_t path_size) {
    uint32_t kind = stream_kind_alias(frame->object_id);
    int written = 0;
    if (use_ring) {
        written = snprintf(path,
                           path_size,
                           "%s/ttp-kind%04x-stream%04x-slot%04d-%ux%u.pgm",
                           out_dir,
                           kind,
                           frame->object_id,
                           slot,
                           frame->width,
                           frame->height);
    } else {
        written = snprintf(path,
                           path_size,
                           "%s/ttp-kind%04x-stream%04x-%06d-%ux%u.pgm",
                           out_dir,
                           kind,
                           frame->object_id,
                           index,
                           frame->width,
                           frame->height);
    }
    if (written < 0 || (size_t)written >= path_size) return -1;
    char tmp_path[1100];
    int tmp_written = snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path);
    if (tmp_written < 0 || (size_t)tmp_written >= sizeof(tmp_path)) return -1;
    FILE *file = fopen(tmp_path, "wb");
    if (file == NULL) {
        perror(tmp_path);
        return -1;
    }
    fprintf(file, "P5\n%u %u\n255\n", frame->width, frame->height);
    for (uint32_t y = 0; y < frame->height; y++) {
        if (fwrite(frame->image + y * frame->stride, 1, frame->width, file) != frame->width) {
            fclose(file);
            return -1;
        }
    }
    fclose(file);
    if (rename(tmp_path, path) != 0) {
        perror(path);
        return -1;
    }
    return 0;
}

static bool stream_enabled(uint16_t stream_id, const uint16_t *streams, size_t stream_count) {
    for (size_t i = 0; i < stream_count; i++) {
        if (streams[i] == stream_id) return true;
    }
    return false;
}

static int write_raw_payload(const char *out_dir,
                             uint32_t object_id,
                             int index,
                             const unsigned char *payload,
                             uint32_t payload_len,
                             char *path,
                             size_t path_size) {
    int written = snprintf(path,
                           path_size,
                           "%s/ttp-stream%04x-%06d-payload.bin",
                           out_dir,
                           object_id & 0xffffu,
                           index);
    if (written < 0 || (size_t)written >= path_size) return -1;
    char tmp_path[1100];
    int tmp_written = snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path);
    if (tmp_written < 0 || (size_t)tmp_written >= sizeof(tmp_path)) return -1;
    FILE *file = fopen(tmp_path, "wb");
    if (file == NULL) {
        perror(tmp_path);
        return -1;
    }
    if (payload_len > 0 && fwrite(payload, 1, payload_len, file) != payload_len) {
        fclose(file);
        return -1;
    }
    fclose(file);
    if (rename(tmp_path, path) != 0) {
        perror(path);
        return -1;
    }
    return 0;
}

static bool parse_streams(const char *text, uint16_t *streams, size_t *stream_count, size_t max_streams) {
    char tmp[128];
    size_t len = strlen(text);
    if (len == 0 || len >= sizeof(tmp)) return false;
    memcpy(tmp, text, len + 1);
    *stream_count = 0;
    for (char *part = strtok(tmp, ","); part != NULL; part = strtok(NULL, ",")) {
        while (isspace((unsigned char)*part)) part++;
        char *end = NULL;
        unsigned long value = strtoul(part, &end, 16);
        if (end == part || value > 0xffff || *stream_count >= max_streams) return false;
        streams[(*stream_count)++] = (uint16_t)value;
    }
    return *stream_count > 0;
}

static void usage(const char *argv0) {
    fprintf(stderr,
            "usage: %s [--label name] [--seconds n] [--samples n] [--csv path]\n"
            "          [--out dir] [--events-csv path] [--streams 050e,1771]\n"
            "          [--image-write-hz n] [--image-ring-size n]\n"
            "          [--startup public|tobiifree] [--file init_hex_path]\n"
            "          [--display-area none|big|rect]\n"
            "          [--display-width-mm n] [--display-height-mm n]\n"
            "          [--display-origin-x-mm n] [--display-origin-y-mm n] [--display-z-mm n]\n"
            "          [--resubscribe-after-timeouts n] [--stream-disable]\n"
            "\n"
            "Runs a native vendor/TTP mux path: interface 0 only, session-open\n"
            "control 0x41, stream setup, then continuous endpoint 0x83 reads\n"
            "for gaze 0x0500 plus image/sync streams such as 0x050e/0x1771.\n"
            "--image-write-hz=0 writes every image packet; positive values cap\n"
            "PGM writes and private_frame_dump output to reduce desktop load.\n"
            "--image-ring-size=0 keeps unique filenames; positive values rotate\n"
            "through a fixed filename ring, intended for tmpfs live buffers.\n"
            "Default startup uses the public stream/display setup known to enable\n"
            "eye detection locally. --startup=tobiifree keeps the minimal\n"
            "hello/realm/subscribe path for diagnostics.\n"
            "--display-area=big applies the tobiifree collection plane\n"
            "TL=(-500,500,0), TR=(500,500,0), BL=(-500,0,0) after startup.\n"
            "--stream-disable also sends the Windows-observed 0x04ce disable\n"
            "request after each non-gaze subscription; diagnostic only.\n",
            argv0);
}

int main(int argc, char **argv) {
    const char *label = "ttp-mux";
    const char *csv_path_arg = NULL;
    const char *out_dir = DEFAULT_IMAGE_OUT;
    const char *events_path_arg = NULL;
    const char *streams_text = "0501,050e,1771";
    const char *startup = "public";
    const char *init_path = DEFAULT_INIT_FILE;
    const char *display_area = "none";
    double display_width_mm = 1000.0;
    double display_height_mm = 500.0;
    double display_origin_x_mm = -500.0;
    double display_origin_y_mm = 0.0;
    double display_z_mm = 0.0;
    double seconds = 10.0;
    double image_write_hz = 0.0;
    int max_samples = 0;
    int image_ring_size = 0;
    int resubscribe_after_timeouts = 0;
    bool stream_disable = false;
    uint16_t streams[8];
    size_t stream_count = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--label") == 0 && i + 1 < argc) {
            label = argv[++i];
        } else if (strcmp(argv[i], "--seconds") == 0 && i + 1 < argc) {
            seconds = atof(argv[++i]);
            if (seconds < 0.0) seconds = 0.0;
        } else if (strcmp(argv[i], "--samples") == 0 && i + 1 < argc) {
            max_samples = atoi(argv[++i]);
            if (max_samples < 0) max_samples = 0;
        } else if (strcmp(argv[i], "--csv") == 0 && i + 1 < argc) {
            csv_path_arg = argv[++i];
        } else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc) {
            out_dir = argv[++i];
        } else if (strcmp(argv[i], "--events-csv") == 0 && i + 1 < argc) {
            events_path_arg = argv[++i];
        } else if (strcmp(argv[i], "--streams") == 0 && i + 1 < argc) {
            streams_text = argv[++i];
        } else if (strcmp(argv[i], "--image-write-hz") == 0 && i + 1 < argc) {
            image_write_hz = atof(argv[++i]);
            if (image_write_hz < 0.0) image_write_hz = 0.0;
        } else if (strcmp(argv[i], "--image-ring-size") == 0 && i + 1 < argc) {
            image_ring_size = atoi(argv[++i]);
            if (image_ring_size < 0) image_ring_size = 0;
        } else if (strcmp(argv[i], "--startup") == 0 && i + 1 < argc) {
            startup = argv[++i];
        } else if (strcmp(argv[i], "--file") == 0 && i + 1 < argc) {
            init_path = argv[++i];
        } else if (strcmp(argv[i], "--display-area") == 0 && i + 1 < argc) {
            display_area = argv[++i];
        } else if (strcmp(argv[i], "--display-width-mm") == 0 && i + 1 < argc) {
            display_width_mm = atof(argv[++i]);
        } else if (strcmp(argv[i], "--display-height-mm") == 0 && i + 1 < argc) {
            display_height_mm = atof(argv[++i]);
        } else if (strcmp(argv[i], "--display-origin-x-mm") == 0 && i + 1 < argc) {
            display_origin_x_mm = atof(argv[++i]);
        } else if (strcmp(argv[i], "--display-origin-y-mm") == 0 && i + 1 < argc) {
            display_origin_y_mm = atof(argv[++i]);
        } else if (strcmp(argv[i], "--display-z-mm") == 0 && i + 1 < argc) {
            display_z_mm = atof(argv[++i]);
        } else if (strcmp(argv[i], "--resubscribe-after-timeouts") == 0 && i + 1 < argc) {
            resubscribe_after_timeouts = atoi(argv[++i]);
            if (resubscribe_after_timeouts < 0) resubscribe_after_timeouts = 0;
        } else if (strcmp(argv[i], "--stream-disable") == 0) {
            stream_disable = true;
        } else if (strcmp(argv[i], "--stream-start") == 0) {
            fprintf(stderr, "warning: --stream-start is deprecated; 0x04ce behaves like disable/unsubscribe\n");
            stream_disable = true;
        } else if (strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return EXIT_SUCCESS;
        } else {
            usage(argv[0]);
            return EXIT_FAILURE;
        }
    }

    if (!parse_streams(streams_text, streams, &stream_count, sizeof(streams) / sizeof(streams[0]))) {
        fprintf(stderr, "invalid --streams value: %s\n", streams_text);
        return EXIT_FAILURE;
    }

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    char csv_path[1024];
    if (csv_path_arg == NULL) {
        if (ensure_default_output_dirs() != 0 ||
            make_default_csv_path(csv_path, sizeof(csv_path), label) != 0) {
            return EXIT_FAILURE;
        }
        csv_path_arg = csv_path;
    }
    if (mkdir_p_recursive(out_dir) != 0) {
        perror(out_dir);
        return EXIT_FAILURE;
    }

    FILE *csv = fopen(csv_path_arg, "w");
    if (csv == NULL) {
        perror(csv_path_arg);
        return EXIT_FAILURE;
    }
    write_csv_header(csv);

    char events_path[1024];
    if (events_path_arg == NULL) {
        int written = snprintf(events_path, sizeof(events_path), "%s/events.csv", out_dir);
        if (written < 0 || (size_t)written >= sizeof(events_path)) {
            fclose(csv);
            return EXIT_FAILURE;
        }
        events_path_arg = events_path;
    }
    FILE *events = fopen(events_path_arg, "w");
    if (events == NULL) {
        perror(events_path_arg);
        fclose(csv);
        return EXIT_FAILURE;
    }
    fprintf(events,
            "elapsed_ms,monotonic_ns,kind,stream,index,device_ts,device_ts2,width,height,stride,bpp,payload_len,path\n");

    libusb_context *ctx = NULL;
    libusb_device_handle *handle = NULL;
    int err = libusb_init(&ctx);
    if (err != 0) {
        print_libusb_error("libusb_init", err);
        fclose(csv);
        return EXIT_FAILURE;
    }
    handle = libusb_open_device_with_vid_pid(ctx, TOBII_VID, TOBII_PID);
    if (handle == NULL) {
        fprintf(stderr, "device_not_found vid=0x%04x pid=0x%04x\n", TOBII_VID, TOBII_PID);
        libusb_exit(ctx);
        fclose(csv);
        fclose(events);
        return EXIT_FAILURE;
    }

    err = libusb_set_auto_detach_kernel_driver(handle, 1);
    if (err != 0 && err != LIBUSB_ERROR_NOT_SUPPORTED) {
        print_libusb_error("libusb_set_auto_detach_kernel_driver", err);
    }

    err = libusb_claim_interface(handle, TOBII_VENDOR_IFACE);
    if (err != 0) {
        print_libusb_error("libusb_claim_interface interface 0", err);
        libusb_close(handle);
        libusb_exit(ctx);
        fclose(csv);
        fclose(events);
        return EXIT_FAILURE;
    }

    err = libusb_control_transfer(handle, 0x41, 0x41, 0, 0, NULL, 0, 1000);
    if (err < 0) {
        print_libusb_error("session_open control 0x41", err);
        libusb_release_interface(handle, TOBII_VENDOR_IFACE);
        libusb_close(handle);
        libusb_exit(ctx);
        fclose(csv);
        fclose(events);
        return EXIT_FAILURE;
    }
    fprintf(stderr, "session_open=ok transferred=%d\n", err);

    struct parser parser = {0};
    uint32_t next_seq = 1;
    if (strcmp(startup, "public") == 0) {
        int command_count = 0;
        err = send_public_init_sequence(handle, &parser, init_path, &command_count);
        if (err != 0) {
            libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
            libusb_release_interface(handle, TOBII_VENDOR_IFACE);
            libusb_close(handle);
            libusb_exit(ctx);
            fclose(csv);
            fclose(events);
            return EXIT_FAILURE;
        }
        fprintf(stderr, "startup=public init_commands=%d file=%s\n", command_count, init_path);
        next_seq = (uint32_t)command_count;
    } else if (strcmp(startup, "tobiifree") == 0) {
        err = run_tobiifree_handshake(handle, &parser, &next_seq);
        if (err != 0) {
            libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
            libusb_release_interface(handle, TOBII_VENDOR_IFACE);
            libusb_close(handle);
            libusb_exit(ctx);
            fclose(csv);
            fclose(events);
            return EXIT_FAILURE;
        }
        err = run_tobiifree_post_connect(handle, &parser, &next_seq);
        if (err != 0) {
            libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
            libusb_release_interface(handle, TOBII_VENDOR_IFACE);
            libusb_close(handle);
            libusb_exit(ctx);
            fclose(csv);
            fclose(events);
            return EXIT_FAILURE;
        }
        fprintf(stderr, "startup=tobiifree\n");
    } else {
        fprintf(stderr, "unknown startup mode: %s\n", startup);
        libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
        libusb_release_interface(handle, TOBII_VENDOR_IFACE);
        libusb_close(handle);
        libusb_exit(ctx);
        fclose(csv);
        fclose(events);
        return EXIT_FAILURE;
    }

    err = send_display_area_override(handle,
                                     &parser,
                                     display_area,
                                     next_seq,
                                     display_width_mm,
                                     display_height_mm,
                                     display_origin_x_mm,
                                     display_origin_y_mm,
                                     display_z_mm);
    if (err != 0) {
        libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
        libusb_release_interface(handle, TOBII_VENDOR_IFACE);
        libusb_close(handle);
        libusb_exit(ctx);
        fclose(csv);
        fclose(events);
        return EXIT_FAILURE;
    }
    if (strcmp(display_area, "none") != 0) {
        next_seq++;
    }

    for (size_t i = 0; i < stream_count; i++) {
        if (streams[i] == STREAM_GAZE) continue;
        err = send_subscribe(handle, next_seq++, streams[i], "ttp_mux");
        if (err != 0) {
            libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
            libusb_release_interface(handle, TOBII_VENDOR_IFACE);
            libusb_close(handle);
            libusb_exit(ctx);
            fclose(csv);
            fclose(events);
            return EXIT_FAILURE;
        }
        if (stream_disable) {
            err = send_unsubscribe(handle, next_seq++, streams[i], "ttp_mux");
            if (err != 0) {
                libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
                libusb_release_interface(handle, TOBII_VENDOR_IFACE);
                libusb_close(handle);
                libusb_exit(ctx);
                fclose(csv);
                fclose(events);
                return EXIT_FAILURE;
            }
        }
    }

    uint64_t start_ns = monotonic_ns();
    uint64_t end_ns = start_ns + (uint64_t)(seconds * 1000000000.0);
    uint64_t image_write_interval_ns = image_write_hz > 0.0 ? (uint64_t)(1000000000.0 / image_write_hz) : 0;
    uint64_t next_image_write_ns = start_ns;
    int reads = 0;
    int timeouts = 0;
    int notifications = 0;
    int gaze_packets = 0;
    int decoded_packets = 0;
    int valid_gaze_packets = 0;
    int left_eye_present_packets = 0;
    int right_eye_present_packets = 0;
    int image_packets = 0;
    int image_written = 0;
    int image_skipped = 0;
    int sync_packets = 0;
    int other_packets = 0;
    int errors = 0;
    int consecutive_timeouts = 0;

    fprintf(stderr,
            "ttp_mux_start streams=%s gaze_csv=%s events_csv=%s out=%s\n",
            streams_text,
            csv_path_arg,
            events_path_arg,
            out_dir);

    while (!stop_requested) {
        uint64_t now_ns = monotonic_ns();
        if (seconds > 0.0 && now_ns >= end_ns) break;
        if (max_samples > 0 && decoded_packets >= max_samples) break;

        struct ttp_frame frame;
        int rc = read_next_frame(handle, &parser, &frame, 250);
        now_ns = monotonic_ns();
        if (rc == 0) {
            timeouts++;
            consecutive_timeouts++;
            if (resubscribe_after_timeouts > 0 && consecutive_timeouts >= resubscribe_after_timeouts) {
                err = send_subscribe(handle, next_seq++, STREAM_GAZE, "timeout_keepalive");
                if (err != 0) {
                    errors++;
                    break;
                }
                consecutive_timeouts = 0;
            }
            continue;
        }
        consecutive_timeouts = 0;
        if (rc < 0) {
            errors++;
            fprintf(stderr, "read_frame=error %s (%d)\n", libusb_error_name(rc), rc);
            break;
        }
        reads++;

        if (frame.magic != TTP_MAGIC_NOTIFY) continue;
        notifications++;
        if (is_image_stream(frame.op)) {
            if (!stream_enabled((uint16_t)frame.op, streams, stream_count)) continue;
            struct image_frame image;
            image_packets++;
            if (image_write_interval_ns > 0 && now_ns < next_image_write_ns) {
                image_skipped++;
                continue;
            }
            if (decode_image_payload(frame.op, frame.payload, frame.plen, &image)) {
                char frame_path[1024];
                int index = image_written;
                int slot = image_ring_size > 0 ? index % image_ring_size : index;
                if (write_pgm_frame(out_dir, &image, index, slot, image_ring_size > 0, frame_path, sizeof(frame_path)) == 0) {
                    image_written++;
                    if (image_write_interval_ns > 0) {
                        next_image_write_ns = now_ns + image_write_interval_ns;
                    }
                    double elapsed_ms = (double)(now_ns - start_ns) / 1000000.0;
                    fprintf(events,
                            "%.3f,%llu,image,0x%04x,%d,%llu,0,%u,%u,%u,%u,%u,%s\n",
                            elapsed_ms,
                            (unsigned long long)now_ns,
                            frame.op,
                            index,
                            (unsigned long long)image.timestamp,
                            image.width,
                            image.height,
                            image.stride,
                            image.bpp,
                            image.payload_len,
                            frame_path);
                    fflush(events);
                    printf("private_frame_dump path=%s\n", frame_path);
                    fflush(stdout);
                }
            }
            continue;
        }
        if (frame.op == STREAM_SYNC) {
            if (!stream_enabled((uint16_t)frame.op, streams, stream_count)) continue;
            struct sync_frame sync;
            sync_packets++;
            if (decode_sync_payload(frame.payload, frame.plen, &sync)) {
                double elapsed_ms = (double)(now_ns - start_ns) / 1000000.0;
                fprintf(events,
                        "%.3f,%llu,sync,0x%04x,%d,%llu,%llu,0,0,0,0,%u,\n",
                        elapsed_ms,
                        (unsigned long long)now_ns,
                        frame.op,
                        sync_packets - 1,
                        (unsigned long long)sync.timestamp,
                        (unsigned long long)sync.receive_timestamp,
                        frame.plen);
                fflush(events);
            }
            continue;
        }
        if (frame.op != STREAM_GAZE) {
            other_packets++;
            if (stream_enabled((uint16_t)frame.op, streams, stream_count)) {
                char payload_path[1024];
                int index = other_packets - 1;
                if (write_raw_payload(out_dir, frame.op, index, frame.payload, frame.plen, payload_path, sizeof(payload_path)) == 0) {
                    double elapsed_ms = (double)(now_ns - start_ns) / 1000000.0;
                    fprintf(events,
                            "%.3f,%llu,other,0x%04x,%d,0,0,0,0,0,0,%u,%s\n",
                            elapsed_ms,
                            (unsigned long long)now_ns,
                            frame.op,
                            index,
                            frame.plen,
                            payload_path);
                    fflush(events);
                }
            }
            continue;
        }
        gaze_packets++;

        struct gaze_sample sample;
        if (!decode_gaze_sample(frame.payload, frame.plen, &sample)) continue;
        if (sample.gaze_valid == 1 && sample.has_gaze_2d && sample.gaze_x >= 0.0 && sample.gaze_y >= 0.0) {
            valid_gaze_packets++;
        }
        if (sample.eye_present_l == 1) left_eye_present_packets++;
        if (sample.eye_present_r == 1) right_eye_present_packets++;
        write_csv_sample(csv, label, start_ns, now_ns, decoded_packets, &frame, &sample);
        decoded_packets++;
        fflush(csv);
    }

    fprintf(stderr,
            "csv=%s\nevents_csv=%s\nsummary label=%s reads=%d timeouts=%d notifications=%d gaze_packets=%d decoded=%d valid_gaze=%d left_eye_present=%d right_eye_present=%d image_packets=%d image_written=%d image_skipped=%d sync_packets=%d other_packets=%d errors=%d\n",
            csv_path_arg,
            events_path_arg,
            label,
            reads,
            timeouts,
            notifications,
            gaze_packets,
            decoded_packets,
            valid_gaze_packets,
            left_eye_present_packets,
            right_eye_present_packets,
            image_packets,
            image_written,
            image_skipped,
            sync_packets,
            other_packets,
            errors);

    libusb_control_transfer(handle, 0x41, 0x42, 0, 0, NULL, 0, 500);
    libusb_release_interface(handle, TOBII_VENDOR_IFACE);
    libusb_close(handle);
    libusb_exit(ctx);
    fclose(csv);
    fclose(events);

    return errors == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
