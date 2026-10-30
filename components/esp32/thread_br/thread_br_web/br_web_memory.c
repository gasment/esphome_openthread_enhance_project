#include "br_web_memory.h"
#include <limits.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include "esp_heap_caps.h"
#include "esp_log.h"

static bool s_prefer_psram;
static size_t s_diag_bytes, s_diag_blocks;
#define DIAG_BYTE_LIMIT (128 * 1024)
#define INTERNAL_RESERVE (32 * 1024)

void br_web_memory_configure(bool prefer_psram) { s_prefer_psram = prefer_psram; }

void *br_web_malloc(size_t size)
{
    void *ptr = NULL;
    if (s_prefer_psram) ptr = heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!ptr && heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) > size + INTERNAL_RESERVE)
        ptr = heap_caps_malloc(size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    return ptr;
}

/* Only raw diagnostic lists use these counters. JSON ownership stays with cJSON. */
void *br_diag_malloc(size_t size)
{
    if (size > DIAG_BYTE_LIMIT || br_diag_live_bytes() > DIAG_BYTE_LIMIT - size) return NULL;
    void *ptr = br_web_malloc(size);
    if (ptr) {
        __atomic_add_fetch(&s_diag_bytes, heap_caps_get_allocated_size(ptr), __ATOMIC_RELAXED);
        __atomic_add_fetch(&s_diag_blocks, 1, __ATOMIC_RELAXED);
    }
    return ptr;
}
void br_diag_free(void *ptr)
{
    if (!ptr) return;
    __atomic_sub_fetch(&s_diag_bytes, heap_caps_get_allocated_size(ptr), __ATOMIC_RELAXED);
    __atomic_sub_fetch(&s_diag_blocks, 1, __ATOMIC_RELAXED);
    free(ptr);
}
size_t br_diag_live_bytes(void) { return __atomic_load_n(&s_diag_bytes, __ATOMIC_RELAXED); }
size_t br_diag_live_blocks(void) { return __atomic_load_n(&s_diag_blocks, __ATOMIC_RELAXED); }
void br_web_log_memory(const char *phase)
{
    ESP_LOGD("br_web_memory", "%s: internal=%u largest=%u psram=%u diagnostic_live=%u bytes/%u blocks",
             phase, (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT),
             (unsigned)br_diag_live_bytes(), (unsigned)br_diag_live_blocks());
}

static cJSON *json_node(int type)
{
    cJSON *node = br_web_malloc(sizeof(cJSON));
    if (node) { memset(node, 0, sizeof(*node)); node->type = type; }
    return node;
}
cJSON *br_json_object(void) { return json_node(cJSON_Object); }
cJSON *br_json_array(void) { return json_node(cJSON_Array); }
cJSON *br_json_string(const char *value)
{
    if (!value) return NULL;
    cJSON *node = json_node(cJSON_String);
    if (!node) return NULL;
    size_t size = strlen(value) + 1;
    node->valuestring = br_web_malloc(size);
    if (!node->valuestring) { free(node); return NULL; }
    memcpy(node->valuestring, value, size);
    return node;
}
cJSON *br_json_number(double value)
{
    cJSON *node = json_node(cJSON_Number);
    if (!node) return NULL;
    node->valuedouble = value;
    node->valueint = !isfinite(value) ? 0 : value >= INT_MAX ? INT_MAX : value <= INT_MIN ? INT_MIN : (int)value;
    return node;
}
bool br_json_add_item(cJSON *object, const char *key, cJSON *item)
{
    if (item && cJSON_AddItemToObjectCS(object, key, item)) return true;
    cJSON_Delete(item);
    return false;
}
bool br_json_append(cJSON *array, cJSON *item)
{
    if (item && cJSON_AddItemToArray(array, item)) return true;
    cJSON_Delete(item);
    return false;
}
cJSON *br_json_add_string(cJSON *object, const char *key, const char *value)
{
    cJSON *item = br_json_string(value);
    return br_json_add_item(object, key, item) ? item : NULL;
}
cJSON *br_json_add_number(cJSON *object, const char *key, double value)
{
    cJSON *item = br_json_number(value);
    return br_json_add_item(object, key, item) ? item : NULL;
}
cJSON *br_json_add_array(cJSON *object, const char *key)
{
    cJSON *item = br_json_array();
    return br_json_add_item(object, key, item) ? item : NULL;
}
