#include "thread_br_ha.h"
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <stdio.h>
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "br_web_memory.h"

static const char *s_url = "", *s_token = "";
static SemaphoreHandle_t s_mutex;
static cJSON *s_names;
static int64_t s_attempt_us, s_success_us;
static int64_t s_retry_us = 30000000;
static bool s_attempted, s_failed;
static thread_br_ha_logger_t s_logger;
static char s_status[160] = "Not queried yet (waiting for BR/Wi-Fi/Native API)";
enum { MAX_RESPONSE = 65536, MAX_IDENTITIES = 256 };

// Resolve the actual entity's HA device, including ESPHome subdevices. Names,
// entity IDs and integration-specific unique_id generation are irrelevant.
static const char *TEMPLATE =
    "{% set ns = namespace(rows=[]) %}"
    "{% for s in states.sensor | selectattr('state', 'match', '^ot1:[0-9a-f]{16}:[0-9a-f]{16}:[0-9a-f]{4}$') %}"
    "{% set d = device_id(s.entity_id) %}"
    "{% set n = device_attr(s.entity_id, 'name_by_user') or device_attr(s.entity_id, 'name') %}"
    "{% if d and n %}{% set ns.rows = ns.rows + [[s.state, n, d]] %}{% endif %}"
    "{% endfor %}{{ ns.rows | to_json }}";

void thread_br_ha_configure(const char *url, const char *token, thread_br_ha_logger_t logger)
{
    s_url = url; s_token = token; s_logger = logger;
    if (*url && *token) {
        s_mutex = xSemaphoreCreateMutex();
        if (!s_mutex && s_logger) s_logger(1, "Cannot allocate HA name cache mutex; lookup disabled");
    }
}

void thread_br_ha_status(char *buffer, size_t capacity)
{
    if (!s_mutex) { snprintf(buffer, capacity, "Lookup disabled"); return; }
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    snprintf(buffer, capacity, "%s", s_status);
    xSemaphoreGive(s_mutex);
}

static bool valid_identity(const char *value)
{
    if (!value || strlen(value) != 42 || memcmp(value, "ot1:", 4) || value[20] != ':' || value[37] != ':') return false;
    for (unsigned i = 4; i < 42; ++i) {
        if (i == 20 || i == 37) continue;
        if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'a' && value[i] <= 'f'))) return false;
    }
    // An unattached or reserved RLOC must never identify a live topology node.
    return strtoul(value + 38, NULL, 16) < 0xfffe;
}

void thread_br_ha_refresh(void)
{
    if (!s_mutex) return;
    int64_t now = esp_timer_get_time();
    if (s_attempted && now - s_attempt_us < s_retry_us) return;
    s_attempted = true; s_attempt_us = now;
    cJSON *request = cJSON_CreateObject(), *rows = NULL;
    char *body = NULL, *url = NULL, *auth = NULL, *response = NULL;
    esp_http_client_handle_t client = NULL;
    bool ok = false, first_success = false;
    int status = 0, identity_count = 0;
    const char *phase = "allocation";
    if (!request || !cJSON_AddStringToObject(request, "template", TEMPLATE)) goto done;
    body = cJSON_PrintUnformatted(request);
    url = malloc(strlen(s_url) + sizeof("/api/template"));
    auth = malloc(strlen(s_token) + sizeof("Bearer "));
    response = br_web_malloc(MAX_RESPONSE + 1);
    if (!body || !url || !auth || !response) goto done;
    strcpy(url, s_url); strcat(url, "/api/template");
    strcpy(auth, "Bearer "); strcat(auth, s_token);
    esp_http_client_config_t config = {
        .url = url, .method = HTTP_METHOD_POST, .timeout_ms = 4000,
        .disable_auto_redirect = true,
#ifdef CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
        .crt_bundle_attach = esp_crt_bundle_attach,
#endif
    };
    client = esp_http_client_init(&config);
    phase = "connect/headers";
    if (!client || esp_http_client_set_header(client, "Authorization", auth) != ESP_OK ||
        esp_http_client_set_header(client, "Content-Type", "application/json") != ESP_OK ||
        esp_http_client_open(client, strlen(body)) != ESP_OK) goto done;
    size_t written = 0, body_length = strlen(body);
    phase = "request write";
    while (written < body_length) {
        int count = esp_http_client_write(client, body + written, body_length - written);
        if (count <= 0) goto done;
        written += count;
    }
    phase = "response headers";
    int64_t length = esp_http_client_fetch_headers(client);
    status = esp_http_client_get_status_code(client);
    if (status != 200 || length > MAX_RESPONSE || length < 0) goto done;
    size_t used = 0;
    phase = "response body";
    // fetch_headers may already have parsed the entire small response, leaving
    // its body in the client's raw buffer. Always drain at least once: parser
    // completion does NOT mean the application has consumed the response.
    while (true) {
        if (used == MAX_RESPONSE || esp_timer_get_time() - now > 10000000) goto done;
        int count = esp_http_client_read(client, response + used, MAX_RESPONSE - used);
        if (count <= 0) {
            if (count == 0 && esp_http_client_is_complete_data_received(client)) break;
            goto done;
        }
        used += count;
        if (esp_http_client_is_complete_data_received(client)) break;
    }
    response[used] = 0;
    phase = "identity JSON";
    rows = cJSON_ParseWithLength(response, used + 1);
    if (!cJSON_IsArray(rows) || cJSON_GetArraySize(rows) > MAX_IDENTITIES) goto done;
    const cJSON *row;
    cJSON_ArrayForEach(row, rows) {
        const cJSON *id = cJSON_GetArrayItem(row, 0), *name = cJSON_GetArrayItem(row, 1), *device = cJSON_GetArrayItem(row, 2);
        if (!cJSON_IsArray(row) || cJSON_GetArraySize(row) != 3 || !cJSON_IsString(id) || !valid_identity(id->valuestring) ||
            !cJSON_IsString(name) || !name->valuestring[0] || strlen(name->valuestring) > 512 ||
            !cJSON_IsString(device) || !device->valuestring[0] || strlen(device->valuestring) > 128) goto done;
    }
    identity_count = cJSON_GetArraySize(rows);
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    cJSON *old = s_names; first_success = old == NULL; s_names = rows; rows = NULL;
    s_success_us = esp_timer_get_time();
    xSemaphoreGive(s_mutex);
    cJSON_Delete(old);
    ok = true;
done:
    if (client) esp_http_client_cleanup(client);
    cJSON_Delete(request); cJSON_Delete(rows); cJSON_free(body);
    free(url); free(auth); free(response);
    char message[160];
    if (ok) snprintf(message, sizeof(message), "HA identity names synchronized: HTTP %d, identities=%d", status, identity_count);
    else snprintf(message, sizeof(message), "HA name refresh failed: phase=%s HTTP=%d; retry with backoff", phase, status);
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    snprintf(s_status, sizeof(s_status), "%s", message);
    xSemaphoreGive(s_mutex);
    // ESP-IDF defaults to ERROR in ESPHome builds. Send our diagnostics through
    // ESPHome's logger rather than silently compiling out IDF INFO/WARN macros.
    if (s_logger && (ok || !s_failed)) s_logger(ok ? ((first_success || s_failed) ? 2 : 3) : 1, message);
    s_retry_us = ok ? 30000000 : (s_retry_us < 150000000 ? s_retry_us * 2 : 300000000);
    s_failed = !ok;
}

// Diagnostic ExtAddress may contain ':' separators; normalize to 16 digits.
static bool normalize_ext(const char *value, char out[17])
{
    unsigned count = 0;
    for (; value && *value; ++value) {
        if (*value == ':' || *value == '-' || *value == ' ') continue;
        if (count == 16 || !isxdigit((unsigned char) *value)) return false;
        out[count++] = (char) tolower((unsigned char) *value);
    }
    out[count] = 0;
    return count == 16;
}

// Caller holds s_mutex. Reject conflicts even if two HA devices have equal names.
static const char *lookup(const char *pan, unsigned rloc, const char *ext)
{
    const char *matched_name = "", *matched_device = NULL, *matched_identity = NULL;
    const cJSON *row;
    cJSON_ArrayForEach(row, s_names) {
        const char *id = cJSON_GetArrayItem(row, 0)->valuestring;
        if (memcmp(id + 4, pan, 16) || strtoul(id + 38, NULL, 16) != rloc ||
            (ext && memcmp(id + 21, ext, 16))) continue;
        const char *device = cJSON_GetArrayItem(row, 2)->valuestring;
        if (matched_device && (strcmp(matched_device, device) || strcmp(matched_identity, id))) return "";
        matched_device = device; matched_identity = id;
        matched_name = cJSON_GetArrayItem(row, 1)->valuestring;
    }
    return matched_name;
}

void thread_br_ha_add_topology_names(cJSON *nodes, const char *pan)
{
    bool locked = s_mutex && xSemaphoreTake(s_mutex, pdMS_TO_TICKS(20)) == pdTRUE;
    // Limit stale RLOC-only mappings after extended HA outages to five minutes.
    bool usable = locked && s_names && esp_timer_get_time() - s_success_us < 300000000;
    cJSON *node;
    cJSON_ArrayForEach(node, nodes) {
        const cJSON *rloc = cJSON_GetObjectItemCaseSensitive(node, "Rloc16");
        const cJSON *ext = cJSON_GetObjectItemCaseSensitive(node, "ExtAddress");
        char normalized[17];
        bool has_ext = cJSON_IsString(ext) && normalize_ext(ext->valuestring, normalized);
        const char *name = usable && cJSON_IsNumber(rloc) ? lookup(pan, rloc->valueint, has_ext ? normalized : NULL) : "";
        br_json_add_string(node, "NodeName", name);
        br_json_add_string(node, "NameSource", "ha_identity");
        cJSON *child;
        cJSON_ArrayForEach(child, cJSON_GetObjectItemCaseSensitive(node, "ChildTable")) {
            const cJSON *id = cJSON_GetObjectItemCaseSensitive(child, "ChildId");
            unsigned child_rloc = cJSON_IsNumber(rloc) && cJSON_IsNumber(id) ?
                                  (rloc->valueint & 0xfc00) | (id->valueint & 0x3ff) : 0xffff;
            br_json_add_string(child, "NodeName", usable ? lookup(pan, child_rloc, NULL) : "");
        }
    }
    if (locked) xSemaphoreGive(s_mutex);
}
