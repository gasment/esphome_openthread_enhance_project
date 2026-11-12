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

## Ready handshake and manual batches

After schema processing and registering all subscriptions, HA responds to the reserved control subscription with HomeAssistantStateResponse `ready`. The node records this on that exact APIConnection. An accepted native Hello alone is not ready. is_ready also requires the initial iterator and deferred state queue to be idle and no send already pending.

send_mode defaults to auto. In manual mode, native SubscribeStates and later entity publishes do not upload state; the send action explicitly runs the existing initial-state iterator once. Event generation is always captured into the bounded RAM queue. A send includes only events with sequence <= the sequence recorded at its start. Those use HomeassistantActionRequest service `thread_sed.batch_event` with the existing event token, without individual ACKs.

After the state iterator/deferred batch drains and included events are written, the node sends `thread_sed.batch`, data token `random_u32:sequence`. HA serializes service-control processing, handles earlier events, saves its state/schema/dedup cache, then replies on thread_sed.control with `batch:<token>`. This is one application ACK for the batch, in addition to TCP's ordinary reliability. The node accepts it only from the selected connection after its marker was written; stale IDs or other connections cannot complete the action. No application packet retransmission layer is added.

Completion invokes the originating action's on_success/on_error branch, whose continuation preserves YAML order even when the branch contains asynchronous actions. A stopped script cancels its wait without later callbacks; already transmitted bytes cannot be recalled. Disconnect, timeout or native send failure retain unconfirmed events. Only one pending send is allowed. The automatic sleep guard waits for a pending send, but manual mode sleep itself does not implicitly transmit unsent states/events.

## Entity selection within a batch

The send action accepts entity_id as either one ESPHome entity ID or a nonempty list of IDs, resolved to EntityBase pointers at code generation. The native InitialStateIterator still advances over registered entities, but skips serialization for unselected pointers. Pointer identity avoids confusing equal object-id hashes in different entity objects. Omitting entity_id retains a full state iteration. Internal and unsupported entities are rejected rather than silently acknowledging a missing state.

The batch owns its selection for the iterator lifetime. Selected queued events share the same batch ACK; removal requires both sequence <= the starting cutoff and membership in the selection. Other events remain queued. HA deduplication is now per boot and event-entity key, retaining the legacy global watermark as a fallback for existing stored entries. This permits selecting B with sequence 2 before A with sequence 1 without suppressing A when it is sent later. No additional entity list is added to network packets; states retain their native keys and the ending marker still yields one ACK.

## Opt-in automatic report confirmation

When component on_ack/on_ack_error automation is configured in auto mode, each accepted native automatic publish increments a local generation. The initial SubscribeStates sync also starts a confirmation cycle. Once the native iterator/deferred queue and existing event ACK queue drain, the node sends the existing thread_sed.batch marker with an auto:<random>:<sequence> token. HA's existing batch:<token> reply is sufficient; there is no new wire format or HA setting.

The marker freezes its covered generation. Later updates remain outstanding even if the marker's ACK arrives. Matching requires the current connection, outstanding marker and exact token. The component callback receives batch_id/elapsed_ms/has_pending on success or batch_id/reason on failure. No snapshot replay is introduced for automatic confirmation, and explicit send snapshots do not themselves enter this callback path. Without configured hooks the monitor and its additional markers are disabled.

A valid application ACK takes precedence over a subsequent connection close. An unconfirmed later write failure is retained for its own cycle. Callback automations execute after resetting transaction state and can publish new values or request sleep. Sleep-marker emission waits for pending automatic confirmation, while the original bounded sleep deadline still prevents an unavailable peer from holding the node awake forever. User-requested asynchronous callback actions are awaited before sleeping.
