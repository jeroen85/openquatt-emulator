#pragma once

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>

namespace oq_native {
// Optional offline-only Ethernet fixture. Every IPv4 egress packet passes the
// allowlist before slirp. TLS remains encrypted and verified by the firmware.
class LocalServices {
 public:
  using Bytes = std::vector<uint8_t>;
  using Clock = std::chrono::steady_clock;
  LocalServices(uint16_t tls_port, uint32_t epoch, uint16_t mqtt_port = 0, bool api = false)
      : tls_port_(tls_port), epoch_(epoch), mqtt_port_(mqtt_port), api_(api) {}
  void clear_forwarded_connections() { peers_.clear(); }
  uint64_t dns_count{0}, ntp_count{0}, denied{0};

  bool outgoing(Bytes& b, const std::function<void(const Bytes&)>& reply, uint32_t elapsed_s = 0,
                Clock::time_point now = Clock::now()) {
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
      if (epoch_ && dst == 123 && b.size() >= p + 56 && (b[p + 8] & 7) == 3) {
        Bytes payload(48, 0);
        payload[0] = 0x24;
        payload[1] = 1;
        payload[2] = 6;
        payload[3] = 0xec;
        std::copy_n(b.begin() + p + 48, 8, payload.begin() + 24);
        put32(payload, 16, epoch_ + elapsed_s + 2208988800U);
        put32(payload, 32, epoch_ + elapsed_s + 2208988800U);
        put32(payload, 40, epoch_ + elapsed_s + 2208988800U);
        ++ntp_count;
        reply(udp_reply(b, p, payload));
        return true;
      }
    }
    if (tcp(b, p) && gateway(b, ip + 16)) {
      if (guest(b, ip + 12) && forwarded(b, p, false, now)) return false;
      const auto destination = word(b, p + 2);
      const uint16_t fixture_port = destination == 443 ? tls_port_ : destination == 1883 ? mqtt_port_ : 0;
      if (fixture_port) {
        put16(b, p + 2, fixture_port);
        checksums(b);
        return false;
      }
    }
    ++denied;
    return true;
  }

  bool incoming(Bytes& b, const std::function<bool(const Bytes&)>& deliver, Clock::time_point now = Clock::now()) {
    if (!ipv4(b) || (word(b, 20) & 0x3fff)) return deliver(b);
    const size_t ip = 14, p = ip + (b[ip] & 15) * 4;
    if (!tcp(b, p) || !gateway(b, ip + 12)) return deliver(b);
    const auto source = word(b, p);
    const uint16_t service_port = source == tls_port_ ? 443 : source == mqtt_port_ ? 1883 : 0;
    if (source && service_port) {
      put16(b, p, service_port);
      checksums(b);
      return deliver(b);
    }
    // A full W5500 receive queue can drop an ACK. Keep forwarding the
    // connection until the guest has actually received the close.
    const bool accepted = deliver(b);
    if (accepted && guest(b, ip + 16)) forwarded(b, p, true, now);
    return accepted;
  }

 private:
  uint16_t tls_port_;
  uint32_t epoch_;
  uint16_t mqtt_port_;
  bool api_;
  struct Peer {
    Clock::time_point touched;
    uint32_t syn_end;
    uint32_t reply_syn_end = 0;
    bool replied = false;
    bool established = false;
    struct HalfClose {
      uint32_t end = 0;
      bool sent = false;
      bool acknowledged = false;
    } close[2];
  };
  std::map<std::pair<uint16_t, uint16_t>, Peer> peers_;
  Clock::time_point next_prune_{};
  static constexpr uint8_t fin = 1, syn = 2, rst = 4, ack = 16;

  bool forwarded(const Bytes& b, size_t p, bool incoming, Clock::time_point now) {
    const auto server = word(b, p + (incoming ? 2 : 0));
    const auto client = word(b, p + (incoming ? 0 : 2));
    if ((server != 80 && !(api_ && server == 6053)) || !client) return false;
    if (now >= next_prune_) {
      next_prune_ = now + std::chrono::seconds(1);
      for (auto it = peers_.begin(); it != peers_.end();) {
        const auto& peer = it->second;
        const auto idle = std::chrono::seconds(!peer.established ? 30 :
            (peer.close[0].sent || peer.close[1].sent) ? 120 : 900);
        if (now - peer.touched >= idle) it = peers_.erase(it);
        else ++it;
      }
    }
    const auto key = std::make_pair(server, client);
    auto it = peers_.find(key);
    const auto flags = b[p + 13] & (fin | syn | rst | ack);
    const auto seq = dword(b, p + 4);
    const auto acknowledged = dword(b, p + 8);
    const auto payload = 14 + word(b, 16) - p - (b[p + 12] >> 4) * 4;
    const uint32_t segment_end = seq + static_cast<uint32_t>(payload) + 1;
    if (incoming && flags == syn) {
      if (it != peers_.end() && it->second.syn_end == segment_end) {
        it->second.touched = now;
      } else if (it != peers_.end() || peers_.size() < 4096) {
        peers_.insert_or_assign(key, Peer{now, segment_end, 0, false, false, {}});
      }
      return true;
    }
    if (it == peers_.end()) return false;
    auto& peer = it->second;
    if (!incoming && flags == (syn | ack) && acknowledged == peer.syn_end) {
      peer.reply_syn_end = segment_end;
      peer.replied = true;
    } else if (!peer.established) {
      if (incoming && (flags & ack) && !(flags & (syn | rst)) && peer.replied &&
          acknowledged == peer.reply_syn_end && seq == peer.syn_end) {
        peer.established = true;
      } else if (!incoming && (flags & rst) && (flags & ack) && acknowledged == peer.syn_end) {
        peers_.erase(it);
        return true;
      } else {
        if (incoming && (flags & rst)) peers_.erase(it);
        return false;
      }
    }
    if (flags & rst) {
      peers_.erase(it);
      return true;
    }
    if (!(flags & ack)) return false;
    auto& sender = peer.close[incoming ? 0 : 1];
    auto& receiver = peer.close[incoming ? 1 : 0];
    if (flags & fin && !sender.sent) {
      sender.end = segment_end;
      sender.sent = true;
    }
    if (receiver.sent && acknowledged == receiver.end) receiver.acknowledged = true;
    peer.touched = now;
    if (sender.acknowledged && receiver.acknowledged) peers_.erase(it);
    return true;
  }

  static bool tcp(const Bytes& b, size_t p) {
    const size_t end = 14 + word(b, 16);
    if (b[23] != 6 || p + 20 > end) return false;
    const size_t header = (b[p + 12] >> 4) * 4;
    return header >= 20 && p + header <= end;
  }

  static bool guest(const Bytes& b, size_t p) {
    return b[p] == 10 && b[p + 1] == 42 && b[p + 2] == 0 && b[p + 3] == 15;
  }

  static uint32_t dword(const Bytes& b, size_t p) { return uint32_t(word(b, p)) << 16 | word(b, p + 2); }
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
