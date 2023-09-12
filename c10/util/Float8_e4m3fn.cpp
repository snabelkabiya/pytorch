#include <c10/util/Float8_e4m3fn.h>
#include <iostream>

namespace c10 {

static_assert(
    std::is_standard_layout<Float8_e4m3fn>::value,
    "c10::Float8_e4m3fn must be standard layout.");

std::ostream& operator<<(std::ostream& out, const Float8_e4m3fn& value) {
  out << (float)value;
  return out;
}
} // namespace c10
