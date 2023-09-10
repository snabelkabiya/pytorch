#include <c10/macros/Macros.h>

#if HAS_DEMANGLE == 0
#include <string>

namespace c10 {
std::string demangle(const char* name) {
  return std::string(name);
}
} // namespace c10
#endif // !HAS_DEMANGLE
