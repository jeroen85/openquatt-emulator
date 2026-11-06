#pragma once
#include <sys/time.h>
namespace cv_virtual_clock {
inline void start() {
  const timeval clock{1793966400, 0};
  settimeofday(&clock, nullptr);
}
}  // namespace cv_virtual_clock
