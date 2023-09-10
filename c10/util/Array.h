#pragma once

#include <array>

namespace c10 {
namespace guts {

template <typename T, int N>
using array = std::array<T, N>;

} // namespace guts
} // namespace c10
