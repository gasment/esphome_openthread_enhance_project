# This module replaces west's OpenThread entry for the current device only.
get_filename_component(sdk_openthread "${ZEPHYR_BASE}/../modules/lib/openthread" ABSOLUTE)
set(poll_patch "${CMAKE_CURRENT_LIST_DIR}/openthread.patch")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${poll_patch}"
  "${CMAKE_CURRENT_LIST_DIR}/prepare.py"
  "${sdk_openthread}/include/openthread/thread.h"
  "${sdk_openthread}/src/core/api/thread_api.cpp"
  "${sdk_openthread}/src/core/mac/data_poll_sender.hpp"
  "${sdk_openthread}/src/core/mac/mac.cpp"
  "${sdk_openthread}/src/core/mac/mac.hpp"
  "${sdk_openthread}/src/core/thread/mle.cpp"
  "${sdk_openthread}/src/core/thread/mle.hpp"
)
execute_process(
  COMMAND "${PYTHON_EXECUTABLE}" "${CMAKE_CURRENT_LIST_DIR}/prepare.py"
    "${sdk_openthread}" "${ZEPHYR_CURRENT_MODULE_DIR}" "${poll_patch}"
  COMMAND_ERROR_IS_FATAL ANY
)
add_subdirectory("${ZEPHYR_BASE}/modules/openthread" zephyr_openthread)
