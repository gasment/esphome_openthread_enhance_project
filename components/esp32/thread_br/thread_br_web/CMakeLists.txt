idf_component_register(
    SRCS "br_web_memory.c" "thread_br_web.c" "esp_br_web_api.c" "esp_br_web_base.c" "thread_br_srp.c" "thread_br_ha.c"
    INCLUDE_DIRS "."
    REQUIRES openthread esp_http_server esp_http_client mbedtls json esp_netif esp_timer esp_app_format esp_hw_support
)
