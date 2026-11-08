#include "local_services.h"
#include "native/odu_bridge.h"

#include <cmath>
#include <cstdio>
#include <stdexcept>

using oq_native::LocalServices;
using Bytes = LocalServices::Bytes;
static void check(bool b) {
  if (!b) throw std::runtime_error("scenario assertion failed");
}
static void put(Bytes& b, size_t p, unsigned n) {
  b[p] = n >> 8;
  b[p + 1] = n;
}
static Bytes packet(int protocol, int src, int dst, size_t payload = 0) {
  Bytes b(14 + 20 + (protocol == 6 ? 20 : 8) + payload, 0);
  put(b, 12, 0x800);
  b[14] = 0x45;
  put(b, 16, b.size() - 14);
  b[22] = 64;
  b[23] = protocol;
  b[26] = 10;
  b[27] = 42;
  b[29] = 15;
  b[30] = 10;
  b[31] = 42;
  b[33] = 2;
  put(b, 34, src);
  put(b, 36, dst);
  if (protocol == 17) put(b, 38, b.size() - 34);
  return b;
}
static uint16_t reg(void* h, uint16_t address) {
  uint16_t v = 0;
  check(oq_odu_read(h, address, &v) == 1);
  return v;
}
int main() {
  try {
    LocalServices local(19499, 1794160000);
    Bytes response;
    auto reply = [&](const Bytes& b) { response = b; };
    auto tls = packet(6, 40000, 443);
    check(!local.outgoing(tls, reply));
    check(tls[36] * 256 + tls[37] == 19499);
    auto escape = packet(6, 40000, 22);
    check(local.outgoing(escape, reply));
    escape = packet(6, 80, 22);
    escape[47] = 0x10;
    check(local.outgoing(escape, reply));
    escape = packet(17, 68, 67);
    escape[30] = 8;
    check(local.outgoing(escape, reply));
    auto ntp = packet(17, 45000, 123, 48);
    ntp[42] = 0x23;
    ntp[82] = 0x43;
    check(local.outgoing(ntp, reply) && local.ntp_count == 1 && response.size() == 90 && response[66] == 0x43);
    Bytes query(12, 0);
    query[5] = 1;
    for (auto label : {std::string("api"), std::string("open-meteo"), std::string("com")}) {
      query.push_back(label.size());
      query.insert(query.end(), label.begin(), label.end());
    }
    query.insert(query.end(), {0, 0, 1, 0, 1});
    auto dns = packet(17, 45000, 53, query.size());
    std::copy(query.begin(), query.end(), dns.begin() + 42);
    check(local.outgoing(dns, reply) && local.dns_count == 1 && response[49] == 1 && response.back() == 2);
    for (size_t n = 0; n < dns.size(); ++n) {
      Bytes truncated(dns.begin(), dns.begin() + n);
      local.outgoing(truncated, reply);
    }
    void* h = oq_odu_create(3, 0);
    check(h != nullptr);
    check(!oq_odu_set_response(h, NAN, 48, 20));
    check(!oq_odu_set_response(h, 48, 48, 0));
    check(oq_odu_set_environment(h, 8, 30, 800, 0, 0, 0));
    check(oq_odu_write(h, 3999, 2, 0));
    check(oq_odu_write(h, 1999, 2, 0));
    check(oq_odu_set_response(h, 48, 48, 20));
    for (uint32_t t = 100; t <= 30000; t += 100) {
      oq_odu_request(h, 1, 1, 3, 2103, 1, t, 0);
      check(oq_odu_advance(h, t));
    }
    check(reg(h, 2102) == 26 && reg(h, 2103) == 48);
    char json[8192];
    check(oq_odu_snapshot(h, json, sizeof(json)) > 0);
    check(oq_odu_write(h, 1999, 0, 30000));
    check(oq_odu_write(h, 3999, 0, 30000));
    for (uint32_t t = 30100; t <= 90000; t += 100) {
      oq_odu_request(h, 1, 1, 3, 2103, 1, t, 0);
      check(oq_odu_advance(h, t));
    }
    check(reg(h, 2103) == 0);
    oq_odu_destroy(h);
    std::puts("Local network isolation, malformed packets and physical ODU response: PASS");
  } catch (const std::exception& e) {
    std::fprintf(stderr, "%s\n", e.what());
    return 1;
  }
}
