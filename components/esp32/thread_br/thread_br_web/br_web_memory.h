#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

void br_web_memory_configure(bool prefer_psram);
void *br_web_malloc(size_t size);
void *br_diag_malloc(size_t size);
void br_diag_free(void *ptr);
size_t br_diag_live_bytes(void);
size_t br_diag_live_blocks(void);
void br_web_log_memory(const char *phase);

/* Local builders with PSRAM-backed nodes/strings, constant object keys and
 * normal cJSON_Delete ownership. Never change process-global cJSON hooks. */
cJSON *br_json_object(void);
cJSON *br_json_array(void);
cJSON *br_json_string(const char *value);
cJSON *br_json_number(double value);
bool br_json_add_item(cJSON *object, const char *key, cJSON *item);
bool br_json_append(cJSON *array, cJSON *item);
cJSON *br_json_add_string(cJSON *object, const char *key, const char *value);
cJSON *br_json_add_number(cJSON *object, const char *key, double value);
cJSON *br_json_add_array(cJSON *object, const char *key);

#ifdef __cplusplus
}
#endif
