# Thread SED protocol v1

Transport is ESPHome native TCP/Protobuf with optional Noise NNpsk0. The fork has an independent C++ namespace and compile guards; the official firmware `api` component is not loaded. Zeroconf/SRP service: `_thread-sed._tcp`, TXT `v=1`, `name`, `mac`, `sleep`, `encryption=none|noise`. Node server defaults to TCP 6053; HA reverse listener uses TCP 6064.

When encryption is enabled, a reverse connection starts with the native unencrypted Noise server hello. Its MAC is used only to locate a configured key. All native messages and side effects follow successful Noise verification. The HA 2026.11 listener peeks at the bounded native server hello without consuming it, then hands the original nonblocking socket to APIClient.start_connection_from_socket() and finish_connection(). The native client performs Noise authentication directly; there is no loopback relay. A key-authenticated Hello with client_info `Thread SED` lets the node remember the real peer address, including IPv6 scope, for future dial-back.

Extensions reuse encrypted native messages, avoiding protobuf registry modifications:

| Direction | Native message | Contents |
|---|---|---|
| Node → HA | HomeassistantActionRequest | `thread_sed.session`, data `schema`, `sleep`, `sleep_seconds` |
| Node → HA | HomeassistantActionRequest | `thread_sed.event`, data `token=boot:seq:key:event_type` |
| Node → HA | HomeassistantActionRequest | `thread_sed.sleep`, data `token=random_u32` |
| HA → Node | HomeAssistantStateResponse | entity_id `thread_sed.control`, state = sleep token or `event:boot:seq` |

These are internal control messages, not arbitrary HA service calls. HA ignores unrecognized service names. Application ACK follows state processing and storage, not TCP delivery alone. Events remain at the head of a 32-item RAM queue until ACK, with 500ms retry. HA deduplicates the latest boot's sequence; queued events are not durable on the device. Ordinary sensor updates use native subscription snapshots on every reconnection.

Before sending a sleep marker the native initial-state iterator and deferred batch must be empty, and the event queue acknowledged. The guard allows sleep after ACK or the bounded delivery deadline. The node intentionally closes TCP before sleeping and dials immediately after wake. Thread mode is unchanged; only data polling is accelerated during the awake window.

Schema cache key combines the configuration fingerprint, firmware compilation time, and ESPHome version. A complete ListEntities response atomically replaces the cache, after which HA reconciles platform entities. Old UDP discovery, binding credentials, manifest revisions, control framing and entity bridge formats are not accepted.

## Plaintext mode

Omitting encryption selects the native plaintext protobuf framing and does not compile the Noise component. The HA client uses noise_psk=None; the onboarding key is optional. Forward TCP uses unmodified native plaintext Hello.

For reverse TCP the device first writes exactly 24 bytes: ASCII `TSED1 ` + the 17-byte colon-separated MAC + LF. This prefix is routing metadata, not authentication. The HA listener peeks to select an existing config entry, rejects a transport/configuration mismatch, then consumes exactly the prefix before passing the socket to the native client. Following native frames are never consumed by the router. Encrypted entries never silently downgrade to plaintext.

The firmware accepts dial-back target Hello in explicitly configured plaintext mode as well as authenticated Noise mode. Entity state, schema, event ACK and sleep ACK messages are identical above the transport layer.
