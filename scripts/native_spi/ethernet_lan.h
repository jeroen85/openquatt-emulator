#pragma once

#include <arpa/inet.h>
#include <libslirp.h>
#include <poll.h>

#include <chrono>
#include <cstdio>
#include <memory>
#include <optional>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
#include "w5500.h"

namespace oq_native {
class EthernetLan {
 public:
  EthernetLan(W5500& chip, int port, const char* cable_path, FILE* trace)
      : chip_(chip), cable_path_(cable_path), trace_(trace) {
    SlirpConfig config{};
    config.version = 6;
    config.restricted = 1;
    config.in_enabled = true;
    config.vnetwork = ip("10.42.0.0");
    config.vnetmask = ip("255.255.255.0");
    config.vhost = ip("10.42.0.2");
    config.vdhcp_start = ip("10.42.0.15");
    config.disable_dns = true;
    config.disable_host_loopback = true;

    callbacks_.send_packet = receive_packet;
    callbacks_.clock_get_ns = [](void*) -> int64_t {
      return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count();
    };
    callbacks_.register_poll_socket = [](slirp_os_socket, void*) {};
    callbacks_.unregister_poll_socket = [](slirp_os_socket, void*) {};
    callbacks_.notify = [](void*) {};
    slirp_.reset(slirp_new(&config, &callbacks_, this));
    if (!slirp_ || slirp_add_hostfwd(slirp_.get(), 0, ip("127.0.0.1"), port, config.vdhcp_start, 80) != 0)
      throw std::runtime_error("Unable to listen on the W5500 HTTP port");
    chip_.transmit = [this](const uint8_t* data, size_t size) {
      if (auto packet = dhcp(data, size, 68, 67); packet && packet->type == 3) requested_ = packet;
      slirp_input(slirp_.get(), data, size);
    };
    read_cable();
  }

  ~EthernetLan() {
    chip_.transmit = {};
    slirp_.reset();
  }

  void service() {
    const auto now = Clock::now();
    if (now < next_poll_) return;
    next_poll_ = now + std::chrono::milliseconds(1);
    if (now >= next_cable_) {
      read_cable();
      next_cable_ = now + std::chrono::milliseconds(20);
    }
    descriptors_.clear();
    uint32_t timeout = 0;
    slirp_pollfds_fill_socket(slirp_.get(), &timeout, add_poll, this);
    const int rc = poll(descriptors_.data(), descriptors_.size(), 0);
    slirp_pollfds_poll(slirp_.get(), rc < 0, poll_result, this);
    if (now >= next_report_) {
      next_report_ = now + std::chrono::seconds(1);
      std::fprintf(trace_,
                   "{\"kind\":\"ethernet\",\"cable\":%s,\"link\":%s,\"tx\":%llu,\"rx\":%llu,\"drops\":%llu,"
                   "\"dhcp_acks\":%llu}\n",
                   chip_.cable ? "true" : "false", chip_.link() ? "true" : "false",
                   static_cast<unsigned long long>(chip_.sent), static_cast<unsigned long long>(chip_.received),
                   static_cast<unsigned long long>(chip_.dropped), static_cast<unsigned long long>(dhcp_acks_));
      std::fflush(trace_);
    }
  }

 private:
  using Clock = std::chrono::steady_clock;
  W5500& chip_;
  std::string cable_path_;
  FILE* trace_;
  SlirpCb callbacks_{};
  std::unique_ptr<Slirp, decltype(&slirp_cleanup)> slirp_{nullptr, slirp_cleanup};
  std::vector<pollfd> descriptors_;
  Clock::time_point next_poll_{}, next_cable_{}, next_report_{};

  struct DhcpPacket {
    std::array<uint8_t, 4> xid{};
    std::array<uint8_t, 6> mac{};
    std::array<uint8_t, 4> address{};
    uint8_t type = 0;
  };
  std::optional<DhcpPacket> requested_;
  uint64_t dhcp_acks_ = 0;

  static uint16_t be16(const uint8_t* bytes) { return (uint16_t(bytes[0]) << 8) | bytes[1]; }

  static std::optional<DhcpPacket> dhcp(const uint8_t* frame, size_t bytes, uint16_t from, uint16_t to) {
    constexpr size_t ethernet = 14, udp_header = 8, options = 240;
    if (bytes < ethernet + 20 || be16(frame + 12) != 0x0800) return std::nullopt;
    const auto* ipv4 = frame + ethernet;
    const size_t header = (ipv4[0] & 15) * 4;
    const size_t total = be16(ipv4 + 2);
    if ((ipv4[0] >> 4) != 4 || header < 20 || ipv4[9] != 17 || (be16(ipv4 + 6) & 0x3fff) || total > bytes - ethernet ||
        total < header + udp_header + options)
      return std::nullopt;
    const auto* udp = ipv4 + header;
    const size_t udp_size = be16(udp + 4);
    if (be16(udp) != from || be16(udp + 2) != to || udp_size < udp_header + options || udp_size > total - header)
      return std::nullopt;
    const auto* payload = udp + udp_header;
    const size_t payload_size = udp_size - udp_header;
    const std::array<uint8_t, 4> cookie{99, 130, 83, 99};
    if (payload[0] != (from == 68 ? 1 : 2) || payload[1] != 1 || payload[2] != 6 ||
        std::memcmp(payload + 236, cookie.data(), cookie.size()))
      return std::nullopt;
    DhcpPacket packet;
    std::copy_n(payload + 4, packet.xid.size(), packet.xid.begin());
    std::copy_n(payload + 16, packet.address.size(), packet.address.begin());
    std::copy_n(payload + 28, packet.mac.size(), packet.mac.begin());
    for (size_t i = options; i < payload_size;) {
      const auto option = payload[i++];
      if (option == 0) continue;
      if (option == 255 || i == payload_size) break;
      const size_t length = payload[i++];
      if (length > payload_size - i) break;
      if (option == 53 && length == 1) {
        packet.type = payload[i];
        return packet;
      }
      i += length;
    }
    return std::nullopt;
  }

  static in_addr ip(const char* text) {
    in_addr value{};
    if (inet_pton(AF_INET, text, &value) != 1) throw std::runtime_error("Invalid fixture network address");
    return value;
  }

  void read_cable() {
    std::unique_ptr<FILE, decltype(&std::fclose)> file(std::fopen(cable_path_.c_str(), "r"), std::fclose);
    if (!file) {
      chip_.cable = false;
      return;
    }
    const int command = std::fgetc(file.get());
    chip_.cable = command == '1';
  }

  static slirp_ssize_t receive_packet(const void* data, size_t size, void* opaque) {
    auto& self = *static_cast<EthernetLan*>(opaque);
    const auto* frame = static_cast<const uint8_t*>(data);
    if (self.chip_.receive(frame, size) && self.requested_) {
      const auto reply = dhcp(frame, size, 67, 68);
      const std::array<uint8_t, 4> expected_ip{10, 42, 0, 15};
      if (reply && reply->type == 5 && reply->xid == self.requested_->xid && reply->mac == self.requested_->mac &&
          reply->address == expected_ip) {
        ++self.dhcp_acks_;
        self.requested_.reset();
      }
    }
    return size;
  }

  static int add_poll(slirp_os_socket fd, int events, void* opaque) {
    auto& items = static_cast<EthernetLan*>(opaque)->descriptors_;
    short mask = 0;
    if (events & SLIRP_POLL_IN) mask |= POLLIN;
    if (events & SLIRP_POLL_OUT) mask |= POLLOUT;
    if (events & SLIRP_POLL_PRI) mask |= POLLPRI;
    items.push_back({fd, mask, 0});
    return static_cast<int>(items.size() - 1);
  }

  static int poll_result(int index, void* opaque) {
    const auto& items = static_cast<EthernetLan*>(opaque)->descriptors_;
    if (index < 0 || static_cast<size_t>(index) >= items.size()) return 0;
    const auto events = items[index].revents;
    int flags = 0;
    if (events & POLLIN) flags |= SLIRP_POLL_IN;
    if (events & POLLOUT) flags |= SLIRP_POLL_OUT;
    if (events & POLLPRI) flags |= SLIRP_POLL_PRI;
    if (events & (POLLERR | POLLNVAL)) flags |= SLIRP_POLL_ERR;
    if (events & POLLHUP) flags |= SLIRP_POLL_HUP;
    return flags;
  }
};
}  // namespace oq_native
