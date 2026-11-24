# Zephyr 3.7 prefers the environment or cache over the application's list.
# Merge these into the application list so native libraries stay registered.
foreach(extra_module_key IN ITEMS EXTRA_ZEPHYR_MODULES ZEPHYR_EXTRA_MODULES)
  if(DEFINED CACHE{${extra_module_key}})
    list(APPEND EXTRA_ZEPHYR_MODULES "$CACHE{${extra_module_key}}")
    unset(${extra_module_key} CACHE)
  endif()
  if(DEFINED ENV{${extra_module_key}})
    list(APPEND EXTRA_ZEPHYR_MODULES "$ENV{${extra_module_key}}")
    unset(ENV{${extra_module_key}})
  endif()
endforeach()
list(APPEND EXTRA_ZEPHYR_MODULES "${CMAKE_CURRENT_LIST_DIR}/..")
list(REMOVE_DUPLICATES EXTRA_ZEPHYR_MODULES)
