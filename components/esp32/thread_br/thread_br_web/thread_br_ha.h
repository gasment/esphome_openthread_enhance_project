#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif
// Strings remain owned by ThreadBorderRouter throughout the worker lifetime.
typedef void (*thread_br_ha_logger_t)(int level, const char *message);
void thread_br_ha_configure(const char *url, const char *token, thread_br_ha_logger_t logger);
void thread_br_ha_status(char *buffer, size_t capacity);
void thread_br_ha_refresh(void);
// No OpenThread lock is held here. ChildTable entries are annotated as well.
void thread_br_ha_add_topology_names(cJSON *nodes, const char *ext_pan_id);

#ifdef __cplusplus
}
#endif
