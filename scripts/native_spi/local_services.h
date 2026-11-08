#pragma once

#include <algorithm>
#include <cstdint>
#include <functional>
#include <string>
#include <set>
#include <vector>

namespace oq_native {
// Optional offline-only Ethernet fixture. Every IPv4 egress packet passes the
// allowlist before slirp. TLS remains encrypted and verified by the firmware.
class LocalServices {
 public:
  using Bytes = std::vector<uint8_t>;
  LocalServices(uint16_t tls_port, uint32_t epoch) : port_(tls_port), epoch_(epoch) {}
  uint64_t dns_count{0}, ntp_count{0}, denied{0};

  bool outgoing(Bytes& b, const std::function<void(const Bytes&)>& reply) {
    if (b.size() < 14) return true;
    if (word(b, 12) == 0x0806) return false;  // ARP, no host IP connection.
    if (!ipv4(b)) return true;
    b.resize(14 + word(b, 16));
    const size_t ip = 14, ihl = (b[ip] & 15) * 4, p = ip + ihl;
    if (word(b, ip + 6) & 0x3fff) return true;  // No fragmented fixture traffic.
    if (b[ip + 9] == 17 && b.size() >= p + 8) {
      const auto src = word(b, p), dst = word(b, p + 2);
      if (src == 68 && dst == 67 && (gateway(b, 30) || (b[30] == 255 && b[31] == 255 && b[32] == 255 && b[33] == 255)))
        return false;
      const size_t length = word(b, p + 4);
      if (length < 8 || p + length > b.size()) return true;
      b.resize(p + length);
      if (dst == 53) {
        Bytes answer;
        if (dns(b, p, answer)) {
          ++dns_count;
          reply(answer);
        }
        return true;
      }
      if (dst == 123 && b.size() >= p + 56 && (b[p + 8] & 7) == 3) {
        Bytes payload(48, 0);
        payload[0] = 0x24;
        payload[1] = 1;
        payload[2] = 6;
        payload[3] = 0xec;
        std::copy_n(b.begin() + p + 48, 8, payload.begin() + 24);
        put32(payload, 16, epoch_ + 2208988800U);
        put32(payload, 32, epoch_ + 2208988800U);
        put32(payload, 40, epoch_ + 2208988800U);
        ++ntp_count;
        reply(udp_reply(b, p, payload));
        return true;
      }
    }
    if (b[ip + 9] == 6 && b.size() >= p + 20 && gateway(b, ip + 16)) {
      if (word(b, p + 2) == 443) {
        put16(b, p + 2, port_);
        checksums(b);
        return false;
      }
      if (word(b, p) == 80 && http_peers_.count(word(b, p + 2)) && (b[p + 13] & 0x10)) return false;
    }
    ++denied;
    return true;
  }

  void incoming(Bytes& b) {
    if (!ipv4(b)) return;
    const size_t ip = 14, p = ip + (b[ip] & 15) * 4;
    if (b[ip + 9] == 6 && b.size() >= p + 20 && gateway(b, ip + 12) && word(b, p + 2) == 80 &&
        http_peers_.size() < 4096)
      http_peers_.insert(word(b, p));
    if (b[ip + 9] == 6 && b.size() >= p + 20 && gateway(b, ip + 12) && word(b, p) == port_) {
      put16(b, p, 443);
      checksums(b);
    }
  }

 private:
  uint16_t port_;
  uint32_t epoch_;
  std::set<uint16_t> http_peers_;
  static uint16_t word(const Bytes& b, size_t p) { return uint16_t(b[p]) << 8 | b[p + 1]; }
  static void put16(Bytes& b, size_t p, uint16_t v) {
    b[p] = v >> 8;
    b[p + 1] = v;
  }
  static void put32(Bytes& b, size_t p, uint32_t v) {
    put16(b, p, v >> 16);
    put16(b, p + 2, v);
  }
  static bool gateway(const Bytes& b, size_t p) {
    return b[p] == 10 && b[p + 1] == 42 && b[p + 2] == 0 && b[p + 3] == 2;
  }
  static bool ipv4(const Bytes& b) {
    if (b.size() < 34 || word(b, 12) != 0x0800 || b[14] >> 4 != 4) return false;
    const size_t ihl = (b[14] & 15) * 4, length = word(b, 16);
    return ihl >= 20 && length >= ihl && b.size() >= 14 + length;
  }
  static uint32_t sum(const Bytes& b, size_t p, size_t length, uint32_t total = 0) {
    for (size_t i = 0; i < length; i += 2) total += uint16_t(b[p + i]) << 8 | (i + 1 < length ? b[p + i + 1] : 0);
    return total;
  }
  static uint16_t finish(uint32_t n) {
    while (n >> 16) n = (n & 65535) + (n >> 16);
    return static_cast<uint16_t>(~n);
  }
  static void checksums(Bytes& b) {
    const size_t ip = 14, ihl = (b[ip] & 15) * 4, p = ip + ihl, length = word(b, ip + 2) - ihl;
    put16(b, ip + 10, 0);
    put16(b, ip + 10, finish(sum(b, ip, ihl)));
    const size_t offset = b[ip + 9] == 6 ? 16 : 6;
    put16(b, p + offset, 0);
    uint16_t checksum = finish(sum(b, p, length, sum(b, ip + 12, 8) + b[ip + 9] + length));
    put16(b, p + offset, checksum == 0 ? 65535 : checksum);
  }
  static Bytes udp_reply(const Bytes& b, size_t p, const Bytes& payload) {
    Bytes out(42 + payload.size(), 0);
    std::copy_n(b.begin() + 6, 6, out.begin());
    std::copy_n(b.begin(), 6, out.begin() + 6);
    put16(out, 12, 0x0800);
    out[14] = 0x45;
    put16(out, 16, out.size() - 14);
    out[22] = 64;
    out[23] = 17;
    std::copy_n(b.begin() + 30, 4, out.begin() + 26);
    std::copy_n(b.begin() + 26, 4, out.begin() + 30);
    put16(out, 34, word(b, p + 2));
    put16(out, 36, word(b, p));
    put16(out, 38, payload.size() + 8);
    std::copy(payload.begin(), payload.end(), out.begin() + 42);
    checksums(out);
    return out;
  }
  static bool dns(const Bytes& b, size_t p, Bytes& answer) {
    const size_t start = p + 8;
    if (b.size() < start + 17 || word(b, start + 4) != 1 || word(b, start + 2) & 0x8000) return false;
    size_t q = start + 12;
    std::string name;
    while (q < b.size() && b[q]) {
      const auto n = b[q++];
      if (n > 63 || q + n >= b.size() || name.size() + n > 253) return false;
      if (!name.empty()) name += '.';
      name.append(reinterpret_cast<const char*>(b.data() + q), n);
      q += n;
    }
    if (++q + 4 > b.size()) return false;
    const bool allowed = name == "github.com" || name == "api.open-meteo.com" || name == "0.pool.ntp.org" ||
                         name == "1.pool.ntp.org" || name == "2.pool.ntp.org" || name == "pool.ntp.org" ||
                         name == "time.google.com" || name == "time.cloudflare.com";
    const bool a = allowed && word(b, q) == 1 && word(b, q + 2) == 1;
    Bytes data(b.begin() + start, b.begin() + q + 4);
    put16(data, 2, allowed ? 0x8180 : 0x8183);
    put16(data, 6, a ? 1 : 0);
    put16(data, 8, 0);
    put16(data, 10, 0);
    if (a) {
      const uint8_t record[]{0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 1, 0, 4, 10, 42, 0, 2};
      data.insert(data.end(), std::begin(record), std::end(record));
    }
    answer = udp_reply(b, p, data);
    return true;
  }
};
}  // namespace oq_native
