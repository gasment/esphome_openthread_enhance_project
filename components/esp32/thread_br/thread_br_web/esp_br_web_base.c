/*
 * SPDX-FileCopyrightText: 2022 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "esp_br_web_base.h"
#include "br_web_memory.h"
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "malloc.h"
#include "stdint.h"
#include "stdio.h"
#include "stdlib.h"
#include "string.h"
#include <stdlib.h>
#include "openthread/border_agent.h"
#include "openthread/dataset.h"
#include "openthread/error.h"
#include "openthread/platform/radio.h"

#define BASE_TAG "web_base"

// convert variable name to string
#define VARIABLE_NAME(name) (#name)
/*---------------------------------------------------------------------------------
                                usual method
---------------------------------------------------------------------------------*/
esp_err_t hex_to_string(const uint8_t hex[], char str[], size_t size)
{
    if (hex == NULL)
        return ESP_FAIL;
    for (int i = 0, j = 0; i < size * 2; i += 2, j++) {
        str[i] = (hex[j] & (0xf0)) >> 4;
        if (str[i] <= 9)
            str[i] = str[i] + '0';
        else
            str[i] = str[i] - 10 + 'a';

        str[i + 1] = (hex[j] & (0x0f));
        if (str[i + 1] <= 9)
            str[i + 1] = str[i + 1] + '0';
        else
            str[i + 1] = str[i + 1] - 10 + 'a';
    }
    str[size * 2] = '\0';
    return ESP_OK;
}

esp_err_t string_to_hex(char str[], uint8_t hex[], size_t size)
{
    if (!str || strlen(str) != size * 2)
        return ESP_FAIL;
    for (int i = 0, j = 0; j < size; i += 2, j++) {
        hex[j] = 0;
        if (str[i] >= '0' && str[i] <= '9')
            hex[j] = (str[i] - '0') << 4;
        else if (str[i] >= 'a' && str[i] <= 'f')
            hex[j] = (str[i] - 'a' + 10) << 4;
        else if (str[i] >= 'A' && str[i] <= 'F')
            hex[j] = (str[i] - 'A' + 10) << 4;
        else
            return ESP_FAIL;

        if (str[i + 1] >= '0' && str[i + 1] <= '9')
            hex[j] = (hex[j] & 0xf0) | ((str[i + 1] - '0') & 0x0f);
        else if (str[i + 1] >= 'a' && str[i + 1] <= 'f')
            hex[j] = (hex[j] & 0xf0) | ((str[i + 1] + 10 - 'a') & 0x0f);
        else if (str[i + 1] >= 'A' && str[i + 1] <= 'F')
            hex[j] = (hex[j] & 0xf0) | ((str[i + 1] + 10 - 'A') & 0x0f);
        else
            return ESP_FAIL;
    }
    return ESP_OK;
}

/*---------------------------------------------------------------------------------
                               thread properties
---------------------------------------------------------------------------------*/
#define CREATE_JSON_CHILD_ITEM(child_json, to_json, from_struct, type, element) \
    cJSON *child_json = cJSON_CreateObject();                                   \
    if (child_json)                                                             \
        cJSON_AddItemToObject(to_json, #element, child_json);

void otbr_properties_reset(openthread_properties_t *properties)
{
    memset(properties, 0x00, sizeof(openthread_properties_t));
}

cJSON *otbr_properties_struct_convert2_json(openthread_properties_t *properties)
{
    cJSON *root = cJSON_CreateObject();

    char address[OT_IP6_ADDRESS_STRING_SIZE];
    char prefix[OT_IP6_PREFIX_STRING_SIZE];
    otIp6AddressToString((const otIp6Address *)&properties->ipv6.link_local_address, address,
                         OT_IP6_ADDRESS_STRING_SIZE);
    cJSON_AddStringToObject(root, "IPv6:LinkLocalAddress", address);

    otIp6AddressToString((const otIp6Address *)&properties->ipv6.routing_local_address, address,
                         OT_IP6_ADDRESS_STRING_SIZE);
    cJSON_AddStringToObject(root, "IPv6:RoutingLocalAddress", address);

    otIp6AddressToString((const otIp6Address *)&properties->ipv6.mesh_local_address, address,
                         OT_IP6_ADDRESS_STRING_SIZE);
    cJSON_AddStringToObject(root, "IPv6:MeshLocalAddress", address);

    otIp6PrefixToString((const otIp6Prefix *)&properties->ipv6.mesh_local_prefix, prefix, OT_IP6_PREFIX_STRING_SIZE);
    cJSON_AddStringToObject(root, "IPv6:MeshLocalPrefix", prefix);

    char format[64];
    sprintf(format, "%s", properties->network.name.m8);
    cJSON_AddStringToObject(root, "Network:Name", format);
    sprintf(format, "0x%x", properties->network.panid);
    cJSON_AddStringToObject(root, "Network:PANID", format);
    sprintf(format, "%lu", properties->network.partition_id);
    cJSON_AddStringToObject(root, "Network:PartitionID", format);
    hex_to_string(properties->network.xpanid.m8, format, sizeof(otExtendedPanId));
    cJSON_AddStringToObject(root, "Network:XPANID", format);
    hex_to_string(properties->network.baid.mId, format, sizeof(otBorderAgentId));
    cJSON_AddStringToObject(root, "Network:BorderAgentID", format);

    cJSON_AddStringToObject(root, "OpenThread:Version", properties->information.version);
    sprintf(format, "%d", properties->information.version_api);
    cJSON_AddStringToObject(root, "OpenThread:Version API", format);
    cJSON_AddStringToObject(root, "Thread:Role", otThreadDeviceRoleToString(properties->information.role));
    hex_to_string(properties->information.PSKc.m8, format, sizeof(otPskc));
    cJSON_AddStringToObject(root, "OpenThread:PSKc", format);

    sprintf(format, "%d", properties->rcp.channel);
    cJSON_AddStringToObject(root, "RCP:Channel", format);
    hex_to_string(properties->rcp.EUI64.m8, format, sizeof(otExtAddress));
    cJSON_AddStringToObject(root, "RCP:EUI64", format);
    sprintf(format, "%d dBm", properties->rcp.txpower);
    cJSON_AddStringToObject(root, "RCP:TxPower", format);
    cJSON_AddStringToObject(root, "RCP:Version", properties->rcp.version);

    cJSON_AddStringToObject(root, "WPAN service", properties->wpan.service);

    /* ESP-IDF version */
    cJSON_AddStringToObject(root, "IDF:Version", esp_get_idf_version());

    /* App / project info */
    const esp_app_desc_t *app_desc = esp_app_get_description();
    if (app_desc) {
        cJSON_AddStringToObject(root, "App:Version", app_desc->version);
        cJSON_AddStringToObject(root, "App:ProjectName", app_desc->project_name);
        char compile_time[48];
        snprintf(compile_time, sizeof(compile_time), "%s %s", app_desc->date, app_desc->time);
        cJSON_AddStringToObject(root, "App:CompileTime", compile_time);
    }

    /* System diagnostics */
    uint32_t uptime_sec = (uint32_t)(esp_timer_get_time() / 1000000);
    uint32_t days = uptime_sec / 86400;
    uint32_t hours = (uptime_sec % 86400) / 3600;
    uint32_t mins = (uptime_sec % 3600) / 60;
    uint32_t secs = uptime_sec % 60;
    if (days > 0) {
        snprintf(format, sizeof(format), "%lud %02lu:%02lu:%02lu", days, hours, mins, secs);
    } else {
        snprintf(format, sizeof(format), "%02lu:%02lu:%02lu", hours, mins, secs);
    }
    cJSON_AddStringToObject(root, "System:Uptime", format);
    snprintf(format, sizeof(format), "%lu KB", (unsigned long)(esp_get_free_heap_size() / 1024));
    cJSON_AddStringToObject(root, "System:FreeHeap", format);

    return root;
}

/*----------------------------------------------------------------------
                       Scan Thread netWork
-----------------------------------------------------------------------*/
cJSON *available_network_struct_convert2_json(thread_network_information_t *network)
{
    cJSON *root = cJSON_CreateObject();

    char format[64];
    cJSON_AddNumberToObject(root, "id", network->id);
    cJSON_AddStringToObject(root, "nn", network->network_name.m8);

    ESP_RETURN_ON_FALSE(!hex_to_string(network->extended_panid.m8, format, sizeof(network->extended_panid.m8)), root,
                        BASE_TAG, "Failed to convert extended panid");

    cJSON_AddStringToObject(root, "ep", format);

    sprintf(format, "0x%04x", network->panid);
    cJSON_AddStringToObject(root, "pi", format);

    ESP_RETURN_ON_FALSE(!hex_to_string(network->extended_address.m8, format, sizeof(network->extended_address.m8)),
                        root, BASE_TAG, "Failed to convert extended address");
    cJSON_AddStringToObject(root, "ha", format);

    cJSON_AddNumberToObject(root, "ch", network->channel);
    cJSON_AddNumberToObject(root, "ri", network->rssi);
    cJSON_AddNumberToObject(root, "li", network->lqi);

    return root;
}

esp_err_t initialize_available_thread_networks_list(thread_network_list_t *list)
{
    list->next = NULL;
    list->network = (thread_network_information_t *)malloc(sizeof(thread_network_information_t));
    ESP_RETURN_ON_FALSE(list->network, ESP_FAIL, BASE_TAG, "Failed to create available thread networks list");
    return ESP_OK;
}

esp_err_t append_available_thread_networks_list(thread_network_list_t *list, thread_network_information_t network)
{
    ESP_RETURN_ON_FALSE(list, ESP_FAIL, BASE_TAG, "Invalid network list");

    thread_network_list_t *head = list;
    while (head != NULL && head->next != NULL) {
        head = head->next;
    }
    thread_network_list_t *node = (thread_network_list_t *)malloc(sizeof(thread_network_list_t));
    if (node != NULL) {
        node->next = NULL;
        node->network = (thread_network_information_t *)malloc(sizeof(thread_network_information_t));
        if (node->network != NULL)
            memcpy(node->network, &network, sizeof(network)); /* need to free */
        else {
            ESP_LOGW(BASE_TAG, "Failed to create network node");
            free(node);
            return ESP_FAIL;
        }
        head->next = node;
    } else {
        ESP_LOGW(BASE_TAG, "Failed to create network list");
        return ESP_FAIL;
    }

    return ESP_OK;
}

void destroy_available_thread_networks_list(thread_network_list_t *list)
{
    if (list == NULL)
        return;
    thread_network_list_t *pre = list;
    thread_network_list_t *next = list->next;
    while (next) {
        free(pre->network);
        free(pre);
        pre = next;
        next = pre->next;
    }
    free(pre->network);
    free(pre); // delete the end node
    list = NULL;
    return;
}

/*----------------------------------------------------------------------
                       Form Thread netWork
-----------------------------------------------------------------------*/
esp_err_t network_formation_param_json_convert2_struct(const cJSON *root, cJSON *log,
                                                       thread_network_formation_param_t *param)
{
    esp_err_t ret = ESP_OK;
    ESP_RETURN_ON_FALSE(log, OT_ERROR_INVALID_ARGS, BASE_TAG, "Invalid argument");
    if (!(root)) {
        ESP_LOGW(BASE_TAG, "Error: Package Type Error");
        cJSON_SetValuestring(log, "Error: Package Type Error");
        return ESP_ERR_INVALID_ARG;
    }

    cJSON *temp = cJSON_GetObjectItem(root, "networkName");
    if (!cJSON_IsString(temp) || strlen(temp->valuestring) > sizeof(otNetworkName)) {
        ESP_LOGW(BASE_TAG, "Error: Invalid Network Name");
        cJSON_SetValuestring(log, "Error: Invalid Network Name");
        return ESP_ERR_INVALID_ARG;
    } else {
        memcpy(param->network_name.m8, temp->valuestring,
               sizeof(otNetworkName) < strlen(temp->valuestring) + 1 ? sizeof(otNetworkName)
                                                                     : strlen(temp->valuestring) + 1);
    }
    if (!(temp = cJSON_GetObjectItem(root, "channel"))) {
        ESP_LOGW(BASE_TAG, "Error: Invalid Channel");
        cJSON_SetValuestring(log, "Error: Invalid Channel");
        return ESP_ERR_INVALID_ARG;
    }

    param->channel = cJSON_GetNumberValue(temp);
    if (param->channel < 11 || param->channel > 26) {
        ESP_LOGW(BASE_TAG, "Error: Channel Out of Range");
        cJSON_SetValuestring(log, "Error: Channel Out of Range");
        return ESP_ERR_INVALID_ARG;
    }

    if (!cJSON_IsString(temp = cJSON_GetObjectItem(root, "panId")) ||
        sscanf(temp->valuestring, "0x%hx", &param->panid) != 1) {
        ESP_LOGW(BASE_TAG, "Error: PanId requires \"0x\"");
        cJSON_SetValuestring(log, "Error: PanId requires  \"0x\"");
        return ESP_ERR_INVALID_ARG;
    }

    if (!(temp = cJSON_GetObjectItem(root, "extPanId")) || strlen(temp->valuestring) != OT_EXT_PAN_ID_SIZE * 2 ||
        string_to_hex(temp->valuestring, param->extended_panid.m8, OT_EXT_PAN_ID_SIZE)) {
        ESP_LOGW(BASE_TAG, "Error: Extended PanId requires 16 bytes");
        cJSON_SetValuestring(log, "Error: Extended PanId requires 16 bytes");
        return ESP_ERR_INVALID_ARG;
    }

    if ((temp = cJSON_GetObjectItem(root, "prefix")) && strlen(temp->valuestring) + 1 < OT_IP6_PREFIX_STRING_SIZE) {
        memcpy(param->on_mesh_prefix, temp->valuestring, strlen(temp->valuestring) + 1);
    }

    if (!(temp = cJSON_GetObjectItem(root, "networkKey")) || strlen(temp->valuestring) != OT_NETWORK_KEY_SIZE * 2 ||
        string_to_hex(temp->valuestring, param->network_key.m8, OT_NETWORK_KEY_SIZE)) {
        ESP_LOGW(BASE_TAG, "Error: Network Key requires 32 bytes");
        cJSON_SetValuestring(log, "Error: Network Key requires 32 bytes");
        return ESP_ERR_INVALID_ARG;
    }

    if ((temp = cJSON_GetObjectItem(root, "passphrase")) &&
        strlen(temp->valuestring) + 1 < NETWORK_PASSPHRASE_MAX_SIZE) {
        memcpy(param->passphrase, temp->valuestring, strlen(temp->valuestring) + 1);
    }

    temp = cJSON_GetObjectItem(root, "defaultRoute");
    if (temp && temp->type != cJSON_NULL)
        param->default_route = (bool)cJSON_GetNumberValue(temp);
    else
        param->default_route = false;

    return ret;
}

void network_formation_param_reset(thread_network_formation_param_t *param)
{
    memset(param, 0x00, sizeof(thread_network_formation_param_t));
}

/*----------------------------------------------------------------------
                       Join Thread netWork
-----------------------------------------------------------------------*/
void network_join_param_reset(thread_network_join_param_t *param)
{
    memset(param, 0x00, sizeof(thread_network_join_param_t));
}

esp_err_t network_join_param_json_convert2_struct(const cJSON *root, cJSON *log, thread_network_join_param_t *param)
{
    cJSON *temp = cJSON_GetObjectItem(root, "index");
    ESP_RETURN_ON_FALSE(log, OT_ERROR_INVALID_ARGS, BASE_TAG, "Invalid argument");
    if ((temp) && temp->valueint >= 0) {
        param->index = temp->valueint;
    } else {
        ESP_LOGW(BASE_TAG, "Error: Invalid Index");
        cJSON_SetValuestring(log, "Error: Invalid Index");
        return ESP_ERR_INVALID_ARG;
    }

    temp = cJSON_GetObjectItem(root, "credentialType");
    if (temp && temp->valuestring && strlen(temp->valuestring) < sizeof(param->credentialType)) {
        memcpy(param->credentialType, temp->valuestring, strlen(temp->valuestring) + 1);
    } else {
        ESP_LOGW(BASE_TAG, "Error: Invalid Credential Type");
        cJSON_SetValuestring(log, "Error: Invalid Credential Type");
        return ESP_ERR_INVALID_ARG;
    }

    temp = cJSON_GetObjectItem(root, "networkKey");
    if (temp && temp->valuestring && strlen(temp->valuestring) == OT_NETWORK_KEY_SIZE * 2) {
        string_to_hex(temp->valuestring, param->networkKey.m8, OT_NETWORK_KEY_SIZE);
    } else {
        ESP_LOGW(BASE_TAG, "Error: Invalid Network Key");
        cJSON_SetValuestring(log, "Error: Invalid Network Key");
        return ESP_ERR_INVALID_ARG;
    }

    temp = cJSON_GetObjectItem(root, "pskd");
    if (temp && temp->valuestring && strlen(temp->valuestring) + 1 < NETWORK_PSKD_MAX_SIZE) {
        memcpy(param->pskd, temp->valuestring, strlen(temp->valuestring) + 1);
    } else {
        ESP_LOGW(BASE_TAG, "Error: Invalid PSKd");
        cJSON_SetValuestring(log, "Error: Invalid PSKd");
        return ESP_ERR_INVALID_ARG;
    }

    temp = cJSON_GetObjectItem(root, "prefix");
    if (temp && temp->valuestring && strlen(temp->valuestring) > 0 &&
        strlen(temp->valuestring) + 1 < OT_IP6_PREFIX_STRING_SIZE) {
        if (!strstr(temp->valuestring, "/")) {
            memcpy(param->prefix, temp->valuestring, strlen(temp->valuestring) + 1);
            strcat(param->prefix, "/64");
        } else {
            memcpy(param->prefix, temp->valuestring, strlen(temp->valuestring) + 1);
        }
    } else {
        /* Prefix is optional for join — leave empty */
        param->prefix[0] = '\0';
    }

    temp = cJSON_GetObjectItem(root, "defaultRoute");
    if (temp && temp->type != cJSON_NULL && temp->valueint)
        param->defaultRoute = true;
    else
        param->defaultRoute = false;
    return ESP_OK;
}

/*----------------------------------------------------------------------
                    Thread network Topology
-----------------------------------------------------------------------*/
esp_err_t initialize_thread_diagnosticTlv_list(thread_diagnosticTlv_list_t *list)
{
    list->next = NULL;
    list->diagTlv = NULL; /* Sentinel owns no TLV storage. */
    return ESP_OK;
}

esp_err_t append_thread_diagnosticTlv_list(thread_diagnosticTlv_list_t *list, otNetworkDiagTlv diagTlv)
{
    ESP_RETURN_ON_FALSE(list, ESP_ERR_INVALID_ARG, BASE_TAG, "Invalid Thread diagnostic list");
    thread_diagnosticTlv_list_t *head = list;
    while (head != NULL && head->next != NULL) { // find the end of list.
        head = head->next;
    }
    thread_diagnosticTlv_list_t *node = (thread_diagnosticTlv_list_t *)br_diag_malloc(sizeof(thread_diagnosticTlv_list_t));
    if (node != NULL) {
        node->next = NULL;
        node->diagTlv = (otNetworkDiagTlv *)br_diag_malloc(sizeof(otNetworkDiagTlv));
        if (node->diagTlv != NULL)
            memcpy(node->diagTlv, &diagTlv, sizeof(otNetworkDiagTlv)); /* need to free */
        else {
            ESP_LOGW(BASE_TAG, "Fail to create diagnostic node!");
            br_diag_free(node);
            return ESP_FAIL;
        }
        head->next = node;
    } else {
        ESP_LOGW(BASE_TAG, "Fail to create diagnostic list node!");
        return ESP_FAIL;
    }
    return ESP_OK;
}

void destroy_thread_diagnosticTlv_list(thread_diagnosticTlv_list_t *list)
{
    if (list == NULL)
        return;
    thread_diagnosticTlv_list_t *pre = list;
    thread_diagnosticTlv_list_t *next = list->next;
    while (next != NULL) {
        br_diag_free(pre->diagTlv);
        br_diag_free(pre);
        pre = next;
        next = pre->next;
    }
    br_diag_free(pre->diagTlv);
    br_diag_free(pre); // delete the end node
    return;
}

esp_err_t initialize_thread_diagnosticTlv_set(thread_diagnosticTlv_set_t *set, const char *rloc16)
{
    ESP_RETURN_ON_FALSE((set && rloc16), ESP_FAIL, BASE_TAG, "Failed to initialize diagnosticTlv set");
    memcpy(&set->rloc16, rloc16, RLOC_STRING_MAX_SIZE);
    time(&set->timeout);
    set->next = NULL;
    set->diagTlv_next = NULL;
    return ESP_OK;
}

esp_err_t update_thread_diagnosticTlv_set(thread_diagnosticTlv_set_t *set, char *rloc16,
                                          thread_diagnosticTlv_list_t *list)
{
    thread_diagnosticTlv_set_t *head = set;
    if (!head) {
        destroy_thread_diagnosticTlv_list(list);
        return ESP_ERR_INVALID_ARG;
    }

    while (head) {
        if (!strcmp(head->rloc16, rloc16)) /* match */
            break;
        head = head->next;
    }

    if (!head) /* name is mismatch, add new list */
    {
        thread_diagnosticTlv_set_t *node = (thread_diagnosticTlv_set_t *)br_diag_malloc(sizeof(thread_diagnosticTlv_set_t));
        if (!node) {
            ESP_LOGW(BASE_TAG, "Fail to malloc dignosticTlv set.");
            destroy_thread_diagnosticTlv_list(list);
            return ESP_ERR_NO_MEM;
        }
        initialize_thread_diagnosticTlv_set(node, rloc16);
        node->diagTlv_next = list;
        time(&node->timeout);
        node->next = NULL;
        head = set;
        while (head->next) head = head->next;
        head->next = node;
        ESP_LOGI(BASE_TAG, "add diagTlv %s to set.", node->rloc16);
    } else /* update diag list */
    {
        destroy_thread_diagnosticTlv_list(head->diagTlv_next);
        head->diagTlv_next = list;
        time(&head->timeout);
        ESP_LOGI(BASE_TAG, "update diagTlv %s.", head->rloc16);
    }
    return ESP_OK;
}

void destroy_thread_diagnosticTlv_set(thread_diagnosticTlv_set_t *set)
{
    if (set == NULL)
        return;
    thread_diagnosticTlv_set_t *pre = set;
    thread_diagnosticTlv_set_t *next = set->next;
    while (next) {
        destroy_thread_diagnosticTlv_list(pre->diagTlv_next);
        pre->diagTlv_next = NULL;
        br_diag_free(pre);
        pre = next;
        next = pre->next;
    }
    destroy_thread_diagnosticTlv_list(pre->diagTlv_next); // destroy the last node
    pre->diagTlv_next = NULL;
    br_diag_free(pre);
    set = NULL;
    return;
}

static cJSON *RouteData2Json(const otNetworkDiagRouteData aRouteData)
{
    cJSON *routeData = br_json_object();

    br_json_add_item(routeData, "RouteId", br_json_number(aRouteData.mRouterId));
    br_json_add_item(routeData, "LinkQualityOut", br_json_number(aRouteData.mLinkQualityOut));
    br_json_add_item(routeData, "LinkQualityIn", br_json_number(aRouteData.mLinkQualityIn));
    br_json_add_item(routeData, "RouteCost", br_json_number(aRouteData.mRouteCost));

    return routeData;
}

static cJSON *Route2Json(const otNetworkDiagRoute aRoute)
{
    cJSON *route = br_json_object();

    br_json_add_item(route, "IdSequence", br_json_number(aRoute.mIdSequence));

    cJSON *RouteData = br_json_array();
    for (uint16_t i = 0; i < aRoute.mRouteCount; ++i) {
        cJSON *RouteDatavalue = RouteData2Json(aRoute.mRouteData[i]);
        br_json_append(RouteData, RouteDatavalue);
    }

    br_json_add_item(route, "RouteData", RouteData);

    return route;
}

static cJSON *LeaderData2Json(const otLeaderData aLeaderData)
{
    cJSON *leaderData = br_json_object();

    br_json_add_item(leaderData, "PartitionId", br_json_number(aLeaderData.mPartitionId));
    br_json_add_item(leaderData, "Weighting", br_json_number(aLeaderData.mWeighting));
    br_json_add_item(leaderData, "DataVersion", br_json_number(aLeaderData.mDataVersion));
    br_json_add_item(leaderData, "StableDataVersion", br_json_number(aLeaderData.mStableDataVersion));
    br_json_add_item(leaderData, "LeaderRouterId", br_json_number(aLeaderData.mLeaderRouterId));

    return leaderData;
}

static cJSON *Mode2Json(const otLinkModeConfig aMode)
{
    cJSON *mode = br_json_object();

    br_json_add_item(mode, "RxOnWhenIdle", br_json_number(aMode.mRxOnWhenIdle));
    br_json_add_item(mode, "DeviceType", br_json_number(aMode.mDeviceType));
    br_json_add_item(mode, "NetworkData", br_json_number(aMode.mNetworkData));

    return mode;
}

static cJSON *Connectivity2Json(const otNetworkDiagConnectivity aConnectivity)
{
    cJSON *connectivity = br_json_object();

    br_json_add_item(connectivity, "ParentPriority", br_json_number(aConnectivity.mParentPriority));
    br_json_add_item(connectivity, "LinkQuality3", br_json_number(aConnectivity.mLinkQuality3));
    br_json_add_item(connectivity, "LinkQuality2", br_json_number(aConnectivity.mLinkQuality2));
    br_json_add_item(connectivity, "LinkQuality1", br_json_number(aConnectivity.mLinkQuality1));
    br_json_add_item(connectivity, "LeaderCost", br_json_number(aConnectivity.mLeaderCost));
    br_json_add_item(connectivity, "IdSequence", br_json_number(aConnectivity.mIdSequence));
    br_json_add_item(connectivity, "ActiveRouters", br_json_number(aConnectivity.mActiveRouters));
    br_json_add_item(connectivity, "SedBufferSize", br_json_number(aConnectivity.mSedBufferSize));
    br_json_add_item(connectivity, "SedDatagramCount", br_json_number(aConnectivity.mSedDatagramCount));

    return connectivity;
}

static cJSON *IpAddr2Json(const otIp6Address aAddress)
{
    char output[64];
    otIp6AddressToString(&aAddress, output, OT_IP6_ADDRESS_STRING_SIZE);
    return br_json_string(output);
}

static cJSON *MacCounters2Json(const otNetworkDiagMacCounters aMacCounters)
{
    cJSON *macCounters = br_json_object();

    br_json_add_item(macCounters, "IfInUnknownProtos", br_json_number(aMacCounters.mIfInUnknownProtos));
    br_json_add_item(macCounters, "IfInErrors", br_json_number(aMacCounters.mIfInErrors));
    br_json_add_item(macCounters, "IfOutErrors", br_json_number(aMacCounters.mIfOutErrors));
    br_json_add_item(macCounters, "IfInUcastPkts", br_json_number(aMacCounters.mIfInUcastPkts));
    br_json_add_item(macCounters, "IfInBroadcastPkts", br_json_number(aMacCounters.mIfInBroadcastPkts));
    br_json_add_item(macCounters, "IfInDiscards", br_json_number(aMacCounters.mIfInDiscards));
    br_json_add_item(macCounters, "IfOutUcastPkts", br_json_number(aMacCounters.mIfOutUcastPkts));
    br_json_add_item(macCounters, "IfOutBroadcastPkts", br_json_number(aMacCounters.mIfOutBroadcastPkts));
    br_json_add_item(macCounters, "IfOutDiscards", br_json_number(aMacCounters.mIfOutDiscards));

    return macCounters;
}

static cJSON *ChildTableEntry2Json(const otNetworkDiagChildEntry aChildEntry)
{
    cJSON *childEntry = br_json_object();

    br_json_add_item(childEntry, "ChildId", br_json_number(aChildEntry.mChildId));
    br_json_add_item(childEntry, "Timeout", br_json_number(aChildEntry.mTimeout));

    cJSON *mode = Mode2Json(aChildEntry.mMode);
    br_json_add_item(childEntry, "Mode", mode);

    return childEntry;
}

cJSON *diagnosticTlv_set_convert2_json(const thread_diagnosticTlv_set_t *set)
{
    ESP_RETURN_ON_FALSE(set, NULL, BASE_TAG, "Invalid Diagnostic Set");
    cJSON *root = br_json_array();
    if (!root) return NULL;
    cJSON *child = NULL;
    cJSON *addr_list = NULL;
    cJSON *table_list = NULL;
    char output[512];
    thread_diagnosticTlv_list_t *list = NULL;
    thread_diagnosticTlv_set_t *head = set->next; /* Skip the invalid header node */
    while (head) {
        child = br_json_object();
        if (!child) { cJSON_Delete(root); return NULL; }
        list = head->diagTlv_next;
        while (list && list->diagTlv) {
            switch (list->diagTlv->mType) {
            case OT_NETWORK_DIAGNOSTIC_TLV_EXT_ADDRESS:
                hex_to_string(list->diagTlv->mData.mExtAddress.m8, output, OT_EXT_ADDRESS_SIZE);
                br_json_add_item(child, "ExtAddress", br_json_string(output));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_SHORT_ADDRESS:
                br_json_add_item(child, "Rloc16", br_json_number(list->diagTlv->mData.mAddr16));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_MODE:
                br_json_add_item(child, "Mode", Mode2Json((list->diagTlv->mData.mMode)));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_TIMEOUT:
                br_json_add_item(child, "Timeout", br_json_number(list->diagTlv->mData.mTimeout));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_CONNECTIVITY:
                br_json_add_item(child, "Connectivity", Connectivity2Json((list->diagTlv->mData.mConnectivity)));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_ROUTE:
                br_json_add_item(child, "Route", Route2Json((list->diagTlv->mData.mRoute)));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_LEADER_DATA:
                br_json_add_item(child, "LeaderData", LeaderData2Json((list->diagTlv->mData.mLeaderData)));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_NETWORK_DATA:
                hex_to_string(list->diagTlv->mData.mNetworkData.m8, output, list->diagTlv->mData.mNetworkData.mCount);
                br_json_add_item(child, "NetworkData", br_json_string(output));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_IP6_ADDR_LIST: {
                addr_list = br_json_array();
                if (list->diagTlv->mData.mIp6AddrList.mCount <= 0 || list->diagTlv->mData.mIp6AddrList.mCount >= 15) {
                    cJSON_Delete(addr_list);
                    addr_list = NULL;
                    break;
                }
                for (uint16_t i = 0; i < list->diagTlv->mData.mIp6AddrList.mCount; ++i) {
                    br_json_append(addr_list, IpAddr2Json((list->diagTlv->mData.mIp6AddrList.mList[i])));
                }
                br_json_add_item(child, "IP6AddressList", addr_list);

            } break;
            case OT_NETWORK_DIAGNOSTIC_TLV_MAC_COUNTERS:
                br_json_add_item(child, "MACCounters", MacCounters2Json((list->diagTlv->mData.mMacCounters)));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_BATTERY_LEVEL:
                br_json_add_item(child, "BatteryLevel", br_json_number(list->diagTlv->mData.mBatteryLevel));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_SUPPLY_VOLTAGE:
                br_json_add_item(child, "SupplyVoltage", br_json_number(list->diagTlv->mData.mSupplyVoltage));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_CHILD_TABLE: {
                table_list = br_json_array();
                for (uint16_t i = 0; i < list->diagTlv->mData.mChildTable.mCount; ++i) {
                    br_json_append(table_list,
                                         ChildTableEntry2Json((list->diagTlv->mData.mChildTable.mTable[i])));
                }
                br_json_add_item(child, "ChildTable", table_list);

            } break;
            case OT_NETWORK_DIAGNOSTIC_TLV_CHANNEL_PAGES:
                hex_to_string(list->diagTlv->mData.mChannelPages.m8, output, list->diagTlv->mData.mChannelPages.mCount);
                br_json_add_item(child, "ChannelPages", br_json_string(output));
                break;
            case OT_NETWORK_DIAGNOSTIC_TLV_MAX_CHILD_TIMEOUT:
                br_json_add_item(child, "MaxChildTimeout",
                                      br_json_number(list->diagTlv->mData.mMaxChildTimeout));
                break;
            default:
                break;
            }
            list = list->next;
        }
        // avoid to add empty child.
        if (head->diagTlv_next && child) {
            br_json_append(root, child);
        } else {
            cJSON_Delete(child);
        }
        head = head->next;
    }
    return root;
}

void thread_node_information_reset(thread_node_information_t *node)
{
    memset(node, 0x00, sizeof(thread_node_information_t));
}

cJSON *thread_node_struct_convert2_json(thread_node_information_t *node)
{
    cJSON *root = cJSON_CreateObject();

    char format[64];
    cJSON_AddStringToObject(root, "NetworkName", node->network_name.m8);

    if (hex_to_string(node->extended_panid.m8, format, OT_EXT_PAN_ID_SIZE)) {
        ESP_LOGW(BASE_TAG, "Fail to convert extended panid.");
        cJSON_Delete(root);
        return NULL;
    }
    cJSON_AddStringToObject(root, "ExtPanId", format);

    if (hex_to_string(node->extended_address.m8, format, OT_EXT_ADDRESS_SIZE)) {
        ESP_LOGW(BASE_TAG, "Fail to convert extended address.");
        cJSON_Delete(root);
        return NULL;
    }
    cJSON_AddStringToObject(root, "ExtAddress", format);

    otIp6AddressToString((const otIp6Address *)&node->rloc_address, format, OT_IP6_ADDRESS_STRING_SIZE);
    cJSON_AddStringToObject(root, "RlocAddress", format);

    cJSON_AddItemToObject(root, "LeaderData", LeaderData2Json((node->leader_data)));

    cJSON_AddNumberToObject(root, "State", node->role);
    cJSON_AddNumberToObject(root, "Rloc16", node->rloc16);
    cJSON_AddNumberToObject(root, "NumOfRouter", node->router_number);

    return root;
}

/*----------------------------------------------------------------------
                       Get Thread dataset
-----------------------------------------------------------------------*/
cJSON *Timestamp2Json(const otTimestamp aTimestamp)
{
    cJSON *timestamp = cJSON_CreateObject();

    cJSON_AddItemToObject(timestamp, "Seconds", cJSON_CreateNumber(aTimestamp.mSeconds));
    cJSON_AddItemToObject(timestamp, "Ticks", cJSON_CreateNumber(aTimestamp.mTicks));
    cJSON_AddItemToObject(timestamp, "Authoritative", cJSON_CreateBool(aTimestamp.mAuthoritative));

    return timestamp;
}

cJSON *SecurityPolicy2Json(const otSecurityPolicy aSecurityPolicy)
{
    cJSON *securityPolicy = cJSON_CreateObject();

    cJSON_AddItemToObject(securityPolicy, "RotationTime", cJSON_CreateNumber(aSecurityPolicy.mRotationTime));
    cJSON_AddItemToObject(securityPolicy, "ObtainNetworkKey",
                          cJSON_CreateBool(aSecurityPolicy.mObtainNetworkKeyEnabled));
    cJSON_AddItemToObject(securityPolicy, "NativeCommissioning",
                          cJSON_CreateBool(aSecurityPolicy.mNativeCommissioningEnabled));
    cJSON_AddItemToObject(securityPolicy, "Routers", cJSON_CreateBool(aSecurityPolicy.mRoutersEnabled));
    cJSON_AddItemToObject(securityPolicy, "ExternalCommissioning",
                          cJSON_CreateBool(aSecurityPolicy.mExternalCommissioningEnabled));
    cJSON_AddItemToObject(securityPolicy, "CommercialCommissioning",
                          cJSON_CreateBool(aSecurityPolicy.mCommercialCommissioningEnabled));
    cJSON_AddItemToObject(securityPolicy, "AutonomousEnrollment",
                          cJSON_CreateBool(aSecurityPolicy.mAutonomousEnrollmentEnabled));
    cJSON_AddItemToObject(securityPolicy, "NetworkKeyProvisioning",
                          cJSON_CreateBool(aSecurityPolicy.mNetworkKeyProvisioningEnabled));
    cJSON_AddItemToObject(securityPolicy, "TobleLink", cJSON_CreateBool(aSecurityPolicy.mTobleLinkEnabled));
    cJSON_AddItemToObject(securityPolicy, "NonCcmRouters", cJSON_CreateBool(aSecurityPolicy.mNonCcmRoutersEnabled));

    return securityPolicy;
}

cJSON *ActiveDataset2Json(const otOperationalDataset aActiveDataset)
{
    cJSON *ActiveDataset = cJSON_CreateObject();
    char format[64];

    if (aActiveDataset.mComponents.mIsActiveTimestampPresent) {
        cJSON_AddItemToObject(ActiveDataset, "ActiveTimestamp", Timestamp2Json(aActiveDataset.mActiveTimestamp));
    }
    if (aActiveDataset.mComponents.mIsNetworkKeyPresent) {
        hex_to_string(aActiveDataset.mNetworkKey.m8, format, OT_NETWORK_KEY_SIZE);
        cJSON_AddStringToObject(ActiveDataset, "NetworkKey", format);
    }
    if (aActiveDataset.mComponents.mIsNetworkNamePresent) {
        cJSON_AddItemToObject(ActiveDataset, "NetworkName", cJSON_CreateString(aActiveDataset.mNetworkName.m8));
    }
    if (aActiveDataset.mComponents.mIsExtendedPanIdPresent) {
        hex_to_string(aActiveDataset.mExtendedPanId.m8, format, OT_EXT_PAN_ID_SIZE);
        cJSON_AddStringToObject(ActiveDataset, "ExtPanId", format);
    }
    if (aActiveDataset.mComponents.mIsMeshLocalPrefixPresent) {
        otIp6Prefix prefix;
        memcpy(prefix.mPrefix.mFields.m8, aActiveDataset.mMeshLocalPrefix.m8, OT_IP6_PREFIX_SIZE);
        prefix.mLength = OT_IP6_PREFIX_SIZE * 8;
        otIp6PrefixToString((const otIp6Prefix *)(&prefix), format, OT_IP6_PREFIX_STRING_SIZE);
        cJSON_AddStringToObject(ActiveDataset, "MeshLocalPrefix", format);
    }
    if (aActiveDataset.mComponents.mIsPanIdPresent) {
        cJSON_AddItemToObject(ActiveDataset, "PanId", cJSON_CreateNumber(aActiveDataset.mPanId));
    }
    if (aActiveDataset.mComponents.mIsChannelPresent) {
        cJSON_AddItemToObject(ActiveDataset, "Channel", cJSON_CreateNumber(aActiveDataset.mChannel));
    }
    if (aActiveDataset.mComponents.mIsPskcPresent) {
        hex_to_string(aActiveDataset.mPskc.m8, format, OT_PSKC_MAX_SIZE);
        cJSON_AddStringToObject(ActiveDataset, "PSKc", format);
    }
    if (aActiveDataset.mComponents.mIsSecurityPolicyPresent) {
        cJSON_AddItemToObject(ActiveDataset, "SecurityPolicy", SecurityPolicy2Json(aActiveDataset.mSecurityPolicy));
    }
    if (aActiveDataset.mComponents.mIsChannelMaskPresent) {
        cJSON_AddItemToObject(ActiveDataset, "ChannelMask", cJSON_CreateNumber(aActiveDataset.mChannelMask));
    }
    return ActiveDataset;
}

cJSON *PendingDataset2Json(const otOperationalDataset aPendingDataset)
{
    cJSON *PendingDataset = cJSON_CreateObject();
    cJSON_AddItemToObject(PendingDataset, "ActiveDataset", ActiveDataset2Json(aPendingDataset));
    if (aPendingDataset.mComponents.mIsPendingTimestampPresent) {
        cJSON_AddItemToObject(PendingDataset, "PendingTimestamp", Timestamp2Json(aPendingDataset.mPendingTimestamp));
    }
    if (aPendingDataset.mComponents.mIsDelayPresent) {
        cJSON_AddItemToObject(PendingDataset, "Delay", cJSON_CreateNumber(aPendingDataset.mDelay));
    }
    return PendingDataset;
}

/*----------------------------------------------------------------------
                       Set Thread dataset
-----------------------------------------------------------------------*/
esp_err_t Json2Timestamp(const cJSON *jsonTimestamp, otTimestamp *aTimestamp)
{
    cJSON *value;

    value = cJSON_GetObjectItemCaseSensitive(jsonTimestamp, "Seconds");
    if (cJSON_IsNumber(value)) {
        aTimestamp->mSeconds = (uint64_t)cJSON_GetNumberValue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonTimestamp, "Ticks");
    if (cJSON_IsNumber(value)) {
        aTimestamp->mTicks = (uint16_t)cJSON_GetNumberValue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonTimestamp, "Authoritative");
    if (cJSON_IsBool(value)) {
        aTimestamp->mAuthoritative = cJSON_IsTrue(value);
    }

    return ESP_OK;
}

esp_err_t Json2SecurityPolicy(const cJSON *jsonSecurityPolicy, otSecurityPolicy *aSecurityPolicy)
{
    cJSON *value = NULL;

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "RotationTime");
    if (cJSON_IsNumber(value)) {
        aSecurityPolicy->mRotationTime = (uint16_t)cJSON_GetNumberValue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "ObtainNetworkKey");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mObtainNetworkKeyEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "NativeCommissioning");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mNativeCommissioningEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "Routers");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mRoutersEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "ExternalCommissioning");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mExternalCommissioningEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "CommercialCommissioning");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mCommercialCommissioningEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "AutonomousEnrollment");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mAutonomousEnrollmentEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "NetworkKeyProvisioning");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mNetworkKeyProvisioningEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "TobleLink");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mTobleLinkEnabled = cJSON_IsTrue(value);
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonSecurityPolicy, "NonCcmRouters");
    if (cJSON_IsBool(value)) {
        aSecurityPolicy->mNonCcmRoutersEnabled = cJSON_IsTrue(value);
    }

    return ESP_OK;
}

esp_err_t Json2ActiveDataset(const cJSON *jsonActiveDataset, otOperationalDataset *aActiveDataset)
{
    cJSON *value = NULL;
    char *str = NULL;

    value = cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "ActiveTimestamp");
    if (cJSON_IsObject(value)) {
        ESP_RETURN_ON_ERROR(Json2Timestamp(value, &(aActiveDataset->mActiveTimestamp)), BASE_TAG,
                            "Invalid ActiveTimestamp");
        aActiveDataset->mComponents.mIsActiveTimestampPresent = true;
    }

    str = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "NetworkKey"));
    if (str != NULL) {
        ESP_RETURN_ON_ERROR(string_to_hex(str, aActiveDataset->mNetworkKey.m8, OT_NETWORK_KEY_SIZE), BASE_TAG,
                            "Invalid Networkkey");
        aActiveDataset->mComponents.mIsNetworkKeyPresent = true;
    }

    str = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "NetworkName"));
    if (str != NULL) {
        ESP_RETURN_ON_FALSE(strlen(str) <= OT_NETWORK_NAME_MAX_SIZE, ESP_ERR_INVALID_ARG, BASE_TAG,
                            "Invalid NetworkName");
        memcpy(aActiveDataset->mNetworkName.m8, str, OT_NETWORK_NAME_MAX_SIZE);
        aActiveDataset->mNetworkName.m8[strlen(str)] = '\0';
        aActiveDataset->mComponents.mIsNetworkNamePresent = true;
    }

    str = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "ExtPanId"));
    if (str != NULL) {
        ESP_RETURN_ON_ERROR(string_to_hex(str, aActiveDataset->mExtendedPanId.m8, OT_EXT_PAN_ID_SIZE), BASE_TAG,
                            "Invalid Extended PanId");
        aActiveDataset->mComponents.mIsExtendedPanIdPresent = true;
    }

    str = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "MeshLocalPrefix"));
    otIp6Prefix prefix;
    if (str != NULL) {
        ESP_RETURN_ON_FALSE(otIp6PrefixFromString(str, &prefix) == OT_ERROR_NONE, ESP_ERR_INVALID_ARG, BASE_TAG,
                            "Invalid MeshLocal Prefix");
        memcpy(aActiveDataset->mMeshLocalPrefix.m8, prefix.mPrefix.mFields.m8, OT_IP6_PREFIX_SIZE);
        aActiveDataset->mComponents.mIsMeshLocalPrefixPresent = true;
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "PanId");
    if (cJSON_IsNumber(value)) {
        aActiveDataset->mPanId = (otPanId)cJSON_GetNumberValue(value);
        aActiveDataset->mComponents.mIsPanIdPresent = true;
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "Channel");
    if (cJSON_IsNumber(value)) {
        uint16_t channel = (uint16_t)cJSON_GetNumberValue(value);
        ESP_RETURN_ON_FALSE(channel >= 11 && channel <= 26, ESP_ERR_INVALID_ARG, BASE_TAG, "Invalid Channel");
        aActiveDataset->mChannel = channel;
        aActiveDataset->mComponents.mIsChannelPresent = true;
    }

    str = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "PSKc"));
    if (str != NULL) {
        ESP_RETURN_ON_ERROR(string_to_hex(str, aActiveDataset->mPskc.m8, OT_PSKC_MAX_SIZE), BASE_TAG, "Invalid PSKc");
        aActiveDataset->mComponents.mIsPskcPresent = true;
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "SecurityPolicy");
    if (cJSON_IsObject(value)) {
        ESP_RETURN_ON_ERROR(Json2SecurityPolicy(value, &(aActiveDataset->mSecurityPolicy)), BASE_TAG,
                            "Invalid SecurityPolicy");
        aActiveDataset->mComponents.mIsSecurityPolicyPresent = true;
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonActiveDataset, "ChannelMask");
    if (cJSON_IsNumber(value)) {
        aActiveDataset->mChannelMask = (otChannelMask)cJSON_GetNumberValue(value);
        aActiveDataset->mComponents.mIsChannelMaskPresent = true;
    }

    return ESP_OK;
}

esp_err_t JsonString2ActiveDataset(const cJSON *JsonStringActiveDataset, otOperationalDataset *aActiveDataset)
{
    otOperationalDatasetTlvs datasetUpdateTlvs;
    char *datasetTlvsStr = cJSON_GetStringValue(JsonStringActiveDataset);
    ESP_RETURN_ON_FALSE(strlen(datasetTlvsStr) <= OT_OPERATIONAL_DATASET_MAX_LENGTH * 2, ESP_ERR_INVALID_ARG, BASE_TAG,
                        "Invalid DatasetTlvs");
    ESP_RETURN_ON_ERROR(string_to_hex(datasetTlvsStr, datasetUpdateTlvs.mTlvs, strlen(datasetTlvsStr) / 2), BASE_TAG,
                        "Invalid DatasetTlvs");
    datasetUpdateTlvs.mLength = strlen(datasetTlvsStr) / 2;
    ESP_RETURN_ON_FALSE(otDatasetParseTlvs(&datasetUpdateTlvs, aActiveDataset) == OT_ERROR_NONE, ESP_ERR_INVALID_ARG,
                        BASE_TAG, "Cannot parse DatasetTlvs");
    return ESP_OK;
}

esp_err_t Json2PendingDataset(const cJSON *jsonPendingDataset, otOperationalDataset *aPendingDataset)
{
    cJSON *value = NULL;

    value = cJSON_GetObjectItemCaseSensitive(jsonPendingDataset, "ActiveDataset");
    if (cJSON_IsObject(value)) {
        ESP_RETURN_ON_ERROR(Json2ActiveDataset(value, aPendingDataset), BASE_TAG, "Invalid ActiveDataset");
    } else if (cJSON_IsString(value)) {
        ESP_RETURN_ON_ERROR(JsonString2ActiveDataset(value, aPendingDataset), BASE_TAG, "Invalid ActiveDataset");
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonPendingDataset, "PendingTimestamp");
    if (cJSON_IsObject(value)) {
        ESP_RETURN_ON_ERROR(Json2Timestamp(value, &(aPendingDataset->mPendingTimestamp)), BASE_TAG,
                            "Invalid PendingTimestamp");
        aPendingDataset->mComponents.mIsPendingTimestampPresent = true;
    }

    value = cJSON_GetObjectItemCaseSensitive(jsonPendingDataset, "Delay");
    if (cJSON_IsNumber(value)) {
        aPendingDataset->mDelay = (uint32_t)cJSON_GetNumberValue(value);
        aPendingDataset->mComponents.mIsDelayPresent = true;
    }
    return ESP_OK;
}

void ot_br_web_response_code_get(uint16_t errcode, char *status_buf)
{
    switch (errcode) {
    case 200:
        strcpy(status_buf, HTTPD_200);
        break;
    case 201:
        strcpy(status_buf, HTTPD_201);
        break;
    case 204:
        strcpy(status_buf, HTTPD_204);
        break;
    case 400:
        strcpy(status_buf, HTTPD_400);
        break;
    case 404:
        strcpy(status_buf, HTTPD_404);
        break;
    case 409:
        strcpy(status_buf, HTTPD_409);
        break;
    default:
        strcpy(status_buf, HTTPD_500);
        break;
    }
}

esp_err_t convert_ot_err_to_response_code(otError errcode, char *status_buf)
{
    esp_err_t err = ESP_OK;
    switch (errcode) {
    case OT_ERROR_NONE:
        strcpy(status_buf, HTTPD_200);
        break;
    case OT_ERROR_INVALID_ARGS:
        strcpy(status_buf, HTTPD_400);
        break;
    case OT_ERROR_INVALID_STATE:
        strcpy(status_buf, HTTPD_409);
        break;
    default:
        ESP_LOGW(BASE_TAG, "None matched http response code, openthread error: %d", errcode);
        err = ESP_ERR_NOT_FOUND;
        break;
    }
    return err;
}
