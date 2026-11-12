#include "udp_transport.h"
#ifdef USE_THREAD_SED_API_UDP
#include "api_server.h"
#include "api_frame_helper_udp.h"
#include "esphome/components/network/util.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include <algorithm>
#include <cerrno>
#include <cstring>

namespace esphome::thread_sed_api {
static const char *const TAG="thread_sed_api.udp";
static uint16_t get16(const uint8_t *p) { return (uint16_t(p[0])<<8)|p[1]; }
static uint32_t get32(const uint8_t *p) { return (uint32_t(p[0])<<24)|(uint32_t(p[1])<<16)|(uint32_t(p[2])<<8)|p[3]; }
static void put16(uint8_t *p,uint16_t n) { p[0]=n>>8; p[1]=n; }
static void put32(uint8_t *p,uint32_t n) { p[0]=n>>24; p[1]=n>>16; p[2]=n>>8; p[3]=n; }
static uint8_t hexbyte(char c) { return c>='0' && c<='9' ? c-'0' : (c|32)-'a'+10; }

void SedUdpTransport::set_key(const std::array<uint8_t,32> &key) {
#ifdef USE_THREAD_SED_UDP_ENCRYPTION
#ifdef USE_THREAD_SED_NRF52
  psa_key_attributes_t attributes=PSA_KEY_ATTRIBUTES_INIT;
  psa_set_key_type(&attributes,PSA_KEY_TYPE_AES);
  psa_set_key_bits(&attributes,256);
  psa_set_key_usage_flags(&attributes,PSA_KEY_USAGE_ENCRYPT|PSA_KEY_USAGE_DECRYPT);
  psa_set_key_algorithm(&attributes,PSA_ALG_GCM);
  if(psa_crypto_init()!=PSA_SUCCESS || psa_import_key(&attributes,key.data(),key.size(),&crypto_key_)!=PSA_SUCCESS)
    owner_->mark_failed();
  psa_reset_key_attributes(&attributes);
#else
  mbedtls_gcm_init(&crypto_);
  if(mbedtls_gcm_setkey(&crypto_,MBEDTLS_CIPHER_ID_AES,key.data(),256)!=0) owner_->mark_failed();
#endif
#endif
}
void SedUdpTransport::setup(uint16_t port) {
  const std::string mac=get_mac_address();
  for(size_t i=0;i<6;i++) mac_[i]=(hexbyte(mac[i*2])<<4)|hexbyte(mac[i*2+1]);
  const char *fingerprint=THREAD_SED_SCHEMA;
  for(size_t i=0;i<16;i++) schema_[i]=(hexbyte(fingerprint[i*2])<<4)|hexbyte(fingerprint[i*2+1]);
  if (!random_bytes(boot_.data(),boot_.size())) { owner_->mark_failed(); return; }
  group_=random_uint32();
  port_=port;
  network_connected_=network::is_connected();
  open_socket_();
  socket_retry_at_=millis();
  preference_=global_preferences->make_preference<SavedPeer>(0x54534432UL,true);
  if(preference_.load(&saved_) && saved_.port!=0) {
    auto *peer=reinterpret_cast<sockaddr_in6 *>(&peer_);
    peer->sin6_family=AF_INET6; peer->sin6_port=htons(HA_PORT); peer->sin6_scope_id=saved_.scope;
    memcpy(&peer->sin6_addr,saved_.address,16); peer_len_=sizeof(*peer);
  }
  auto *client=owner_->add_client_(nullptr);
  client->sed_udp_ready(network_connected_ && ready());
  ESP_LOGCONFIG(TAG,"UDP port %u; cached HA endpoint: %s",port,ready()?"YES":"NO");
}
bool SedUdpTransport::open_socket_() {
  // Recreate the native receive context after a Thread detach/reattach, even
  // if the OMR address and port are identical to the previous attachment.
  socket_.reset();
  auto sock=socket::socket_loop_monitored(AF_INET6,SOCK_DGRAM,IPPROTO_UDP);
  sockaddr_storage local{};
  const auto len=socket::set_sockaddr_any(reinterpret_cast<sockaddr *>(&local),sizeof(local),port_);
  if(!sock || len==0 || sock->setblocking(false)!=0 ||
      sock->bind(reinterpret_cast<sockaddr *>(&local),len)!=0) {
    ESP_LOGW(TAG,"UDP socket bind failed on port %u: errno=%d; will retry",port_,errno);
    return false;
  }
  socket_=std::move(sock);
  return true;
}
void SedUdpTransport::reset_delivery_(const char *reason) {
  // Notify owners in the regular sed_tick() path, never from a network callback.
  // Their cached events remain available for a later send.
  if(owner_->sed_batch_done_) owner_->sed_batch_delivery_error_=reason;
  if(owner_->sed_auto_pending()) owner_->sed_auto_error_=reason;
  owner_->clients_[0]->sed_cancel_snapshot();
  cancel();
}
void SedUdpTransport::bind_peer_(const sockaddr_storage &peer,socklen_t length) {
  peer_=peer; peer_len_=length;
  auto *ip=reinterpret_cast<sockaddr_in6 *>(&peer_);
  // HA's business listener is fixed at 6064. probe_device() uses a temporary
  // source port which must never be persisted as the business destination.
  ip->sin6_port=htons(HA_PORT);
  SavedPeer saved{};
  memcpy(saved.address,&ip->sin6_addr,16); saved.port=ip->sin6_port; saved.scope=ip->sin6_scope_id;
  if(memcmp(&saved,&saved_,sizeof(saved))!=0) {
    saved_=saved; preference_.save(&saved_);
  }
  owner_->clients_[0]->sed_udp_ready(true);
}
bool SedUdpTransport::append(uint16_t type,const uint8_t *data,size_t size) {
  if(!ready() || size>65535 || staging_.size()+size+4>MAX_BODY) return false;
  if(staging_.empty()) staged_at_=millis();
  const auto offset=staging_.size(); staging_.resize(offset+4+size);
  put16(staging_.data()+offset,type); put16(staging_.data()+offset+2,size);
  if(size) memcpy(staging_.data()+offset+4,data,size);
  return true;
}
bool SedUdpTransport::flush(const std::string &control, uint32_t ack_timeout, uint16_t ack_resend) {
  if(!tx_.empty()) return false;
  if(staging_.empty()) return true;
  tx_=std::move(staging_); staging_.clear();
  tx_started_=millis();
  tx_ack_timeout_=ack_timeout; tx_ack_resend_=ack_resend;
  tx_control_=control; tx_group_=++group_; attempts_=0; tx_part_=0;
  tx_flags_=schema_building_?2:0;
  tx_peer_=(tx_flags_&2)?schema_peer_:peer_;
  tx_peer_len_=(tx_flags_&2)?schema_peer_len_:peer_len_;
  if(tx_flags_&2) ESP_LOGI(TAG,"Sending descriptors: group=%u bytes=%u fragments=%u",
      static_cast<unsigned>(tx_group_),static_cast<unsigned>(tx_.size()),
      static_cast<unsigned>((tx_.size()+CHUNK-1)/CHUNK));
  transmit_();
  return true;
}
bool SedUdpTransport::send_(uint8_t kind,uint32_t group,uint16_t part,uint16_t parts,
                            const uint8_t *data,size_t size,uint8_t flags,
                            const sockaddr_storage *destination,socklen_t destination_length) {
  if(!ready() || size>CHUNK) return false;
  std::vector<uint8_t> packet(HEADER+size+16);
  memcpy(packet.data(),"TSD2",4); packet[4]=kind; packet[5]=flags;
  memcpy(packet.data()+6,mac_.data(),6); memcpy(packet.data()+12,boot_.data(),8);
  if(++packet_sequence_==0) { if (!random_bytes(boot_.data(),boot_.size())) { owner_->mark_failed(); return false; } memcpy(packet.data()+12,boot_.data(),8); }
  put32(packet.data()+20,packet_sequence_); put32(packet.data()+24,group);
  put16(packet.data()+28,part); put16(packet.data()+30,parts); memcpy(packet.data()+32,schema_.data(),16);
#ifdef USE_THREAD_SED_UDP_ENCRYPTION
  packet[5]|=1;
#ifdef USE_THREAD_SED_NRF52
  size_t encrypted_size=0;
  if(psa_aead_encrypt(crypto_key_,PSA_ALG_GCM,packet.data()+12,12,packet.data(),HEADER,
      data,size,packet.data()+HEADER,size+16,&encrypted_size)!=PSA_SUCCESS) return false;
  size=encrypted_size;
#else
  if(mbedtls_gcm_crypt_and_tag(&crypto_,MBEDTLS_GCM_ENCRYPT,size,packet.data()+12,12,
      packet.data(),HEADER,data,packet.data()+HEADER,16,packet.data()+HEADER+size)!=0) return false;
  size+=16;
#endif
#else
  if(size) memcpy(packet.data()+HEADER,data,size);
#endif
  const auto *target=destination?destination:&peer_;
  const auto target_len=destination?destination_length:peer_len_;
  return socket_->sendto(packet.data(),HEADER+size,0,reinterpret_cast<const sockaddr *>(target),target_len)==static_cast<ssize_t>(HEADER+size);
}
void SedUdpTransport::transmit_() {
  if(tx_.empty()) return;
  const size_t parts=(tx_.size()+CHUNK-1)/CHUNK;
  if(tx_part_>=parts) return;
  const auto size=std::min(CHUNK,tx_.size()-tx_part_*CHUNK);
  // One datagram per loop, with backpressure. A large schema must not keep
  // resending its first fragment while starving the later ones on EAGAIN.
  if(send_(2,tx_group_,tx_part_,parts,tx_.data()+tx_part_*CHUNK,size,tx_flags_,&tx_peer_,tx_peer_len_)) ++tx_part_;
  if(tx_part_==parts) {
    last_send_=millis(); ++attempts_;
    ESP_LOGD(TAG,"UDP group=%u round=%u/%u sent; waiting ACK %u ms (port=%u)",
        static_cast<unsigned>(tx_group_),static_cast<unsigned>(attempts_),static_cast<unsigned>(tx_ack_resend_+1U),
        static_cast<unsigned>(tx_ack_timeout_),ntohs(reinterpret_cast<const sockaddr_in6 *>(&tx_peer_)->sin6_port));
  }
}
void SedUdpTransport::request_schema(const sockaddr_storage *reply_peer,socklen_t reply_length) {
  requested_schema_peer_=reply_peer?*reply_peer:peer_;
  requested_schema_peer_len_=reply_peer?reply_length:peer_len_;
  schema_requested_=true;
}
void SedUdpTransport::cancel() {
  staging_.clear(); tx_.clear(); tx_control_.clear(); paused_tx_.clear(); paused_control_.clear();
  schema_building_=false; schema_requested_=false;
}
void SedUdpTransport::fail_(const char *reason) {
  // Propagate failure to the owning transaction; callbacks run in sed_tick(),
  // after all transport buffers have been released.
  const auto &control=paused_control_.empty()?tx_control_:paused_control_;
  if(owner_->sed_batch_done_ && control=="batch:"+owner_->sed_batch_token_)
    owner_->sed_batch_delivery_error_=reason;
  if(owner_->sed_auto_pending() && control=="batch:"+owner_->sed_auto_token_)
    owner_->sed_auto_error_=reason;
  ESP_LOGW(TAG,"UDP delivery failed: %s",reason);
  cancel();
}
void SedUdpTransport::wake() {
  // A fresh wake nonce rejects commands left in a parent/router queue while
  // the main thread was asleep. It also gives AES-GCM a fresh nonce domain.
  if (!random_bytes(boot_.data(),boot_.size())) { owner_->mark_failed(); return; }
  packet_sequence_=0;
  owner_->clients_[0]->sed_udp_ready(ready());
  if (ready() && !owner_->sed_manual_send()) owner_->clients_[0]->on_subscribe_states_request();
}
void SedUdpTransport::sleep() {
  uint8_t duration[4]; put32(duration,owner_->sed_sleep_seconds());
  send_(6,++group_,0,1,duration,sizeof(duration));
}
void SedUdpTransport::receive_() {
  // Bounded work and heap-backed buffer: do not place a full datagram on the main stack.
  std::vector<uint8_t> packet(HEADER+CHUNK+16);
  for(unsigned work=0;work<8;work++) {
    packet.resize(HEADER+CHUNK+16);
    sockaddr_storage from{}; socklen_t length=sizeof(from);
    const int count=socket_->recvfrom(packet.data(),packet.size(),reinterpret_cast<sockaddr *>(&from),&length);
    if(count<0) break;
    if(count<int(HEADER) || memcmp(packet.data(),"TSD2",4)!=0) continue;
    auto *p=packet.data(); const uint8_t kind=p[4],flags=p[5];
    if(kind<1 || kind>5 || flags&~3 || get16(p+30)!=1 || get16(p+28)!=0) continue;
    if(kind==1) {
      char source[socket::SOCKADDR_STR_LEN];
      socket::format_sockaddr_to(reinterpret_cast<const sockaddr *>(&from),length,source);
      ESP_LOGI(TAG,"UDP probe received from [%s]:%u (encrypted=%s)",source,
          ntohs(reinterpret_cast<const sockaddr_in6 *>(&from)->sin6_port),(flags&1)?"YES":"NO");
    }
    const bool wildcard=std::all_of(p+6,p+12,[](uint8_t n){return n==0;});
    if(memcmp(p+6,mac_.data(),6)!=0 && !(kind==1 && wildcard)) {
      if(kind==1) ESP_LOGW(TAG,"Probe rejected: target MAC does not match this node");
      continue;
    }
    size_t size=count-HEADER;
#ifdef USE_THREAD_SED_UDP_ENCRYPTION
    if(!(flags&1) || size<16) {
      if(kind==1) ESP_LOGW(TAG,"Probe rejected: this node requires UDP encryption");
      continue;
    }
    size-=16;
    std::vector<uint8_t> decoded(size+1);
#ifdef USE_THREAD_SED_NRF52
    size_t decoded_size=0;
    if(psa_aead_decrypt(crypto_key_,PSA_ALG_GCM,p+12,12,p,HEADER,p+HEADER,size+16,
        decoded.data(),decoded.size(),&decoded_size)!=PSA_SUCCESS || decoded_size!=size) continue;
#else
    if(mbedtls_gcm_auth_decrypt(&crypto_,size,p+12,12,p,HEADER,p+HEADER+size,16,p+HEADER,decoded.data())!=0) continue;
#endif
    memcpy(p+HEADER,decoded.data(),size);
#else
    if(flags&1) {
      if(kind==1) ESP_LOGW(TAG,"Probe rejected: this node does not enable UDP encryption");
      continue;
    }
#endif
    if(kind==1) {
      const bool was_ready=ready();
      bind_peer_(from,length);
      if(memcmp(p+32,schema_.data(),16)!=0) request_schema(&from,length);
      else {
        send_(7,++group_,0,1,nullptr,0,0,&from,length);
        if(!was_ready && !owner_->sed_manual_send()) owner_->clients_[0]->on_subscribe_states_request();
      }
      continue;
    }
    // Match ACKs to the frozen recipient of this group, not the most recent
    // PROBE source. Commands/schema requests use the stable business endpoint.
    const auto *expected=reinterpret_cast<const sockaddr_in6 *>((kind==3 && !tx_.empty())?&tx_peer_:&peer_);
    const auto *sender=reinterpret_cast<const sockaddr_in6 *>(&from);
    if(!ready() || sender->sin6_port!=expected->sin6_port ||
        memcmp(&sender->sin6_addr,&expected->sin6_addr,16)!=0) continue;
    const uint32_t group=get32(p+24);
    if(kind==4) { request_schema(); continue; }
    if(kind==3 && !tx_.empty() && group==tx_group_ && size==8 && memcmp(p+HEADER,boot_.data(),8)==0) {
      ESP_LOGD(TAG,"UDP group=%u ACK after %u round(s), %u ms since last round",static_cast<unsigned>(group),
          static_cast<unsigned>(attempts_),static_cast<unsigned>(millis()-last_send_));
      const auto control=tx_control_; const bool schema=(tx_flags_&2)!=0;
      tx_.clear(); tx_control_.clear();
      if(schema) {
        ESP_LOGI(TAG,"Descriptor ACK received: group=%u",static_cast<unsigned>(group));
        schema_building_=false;
        if(!paused_tx_.empty()) {
          tx_=std::move(paused_tx_); paused_tx_.clear(); tx_group_=paused_group_;
          tx_control_=std::move(paused_control_); paused_control_.clear();
          tx_peer_=paused_peer_; tx_peer_len_=paused_peer_len_;
          tx_ack_timeout_=paused_ack_timeout_; tx_ack_resend_=paused_ack_resend_; tx_flags_=0; attempts_=0; tx_part_=0; tx_started_=millis(); transmit_();
        }
      }
      if(schema && paused_tx_.empty() && tx_.empty() && !owner_->sed_manual_send())
        owner_->clients_[0]->on_subscribe_states_request();
      if(!control.empty()) owner_->sed_control(owner_->clients_[0].get(),StringRef(control));
      continue;
    }
    if(kind==5 && size>=12 && memcmp(p+HEADER,boot_.data(),8)==0) {
      std::array<uint8_t,16> identity{}; memcpy(identity.data(),p+12,8); put32(identity.data()+8,group);
      bool duplicate=false; for(const auto &known:commands_) if(known==identity) duplicate=true;
      if(!duplicate) {
        // Commands are one record per datagram, with an explicit allowlist.
        const uint16_t type=get16(p+HEADER+8),bytes=get16(p+HEADER+10);
        const bool allowed=type==30 || type==31 || type==32 || type==33 || type==48 || type==51 ||
            type==54 || type==60 || type==62 || type==99 || type==111;
        if(bytes+12!=size || !allowed) continue;
        // In-place protobuf strings require one extra terminator byte.
        packet.resize(HEADER+size+1); packet[HEADER+size]=0;
        owner_->clients_[0]->read_message_(bytes,type,packet.data()+HEADER+12);
        commands_[command_cursor_++%commands_.size()]=identity;
      }
      send_(3,group,0,1,p+12,8);
    }
  }
}
void SedUdpTransport::loop() {
  const bool connected=network::is_connected();
  if(connected!=network_connected_) {
    network_connected_=connected;
    reset_delivery_(connected?"thread_reconnected":"thread_disconnected");
    socket_.reset();
    owner_->clients_[0]->sed_udp_ready(false);
    socket_retry_at_=millis()-1000U;
    if(connected) {
      if(!random_bytes(boot_.data(),boot_.size())) { owner_->mark_failed(); return; }
      packet_sequence_=0;
      commands_={}; command_cursor_=0;
      ESP_LOGI(TAG,"Thread reattached; reopening UDP port %u and resetting unfinished transfers",port_);
    } else {
      ESP_LOGI(TAG,"Thread detached; UDP receive context and unfinished transfers cleared");
    }
  }
  if(!connected) return;
  if(!socket_) {
    if(millis()-socket_retry_at_<1000U) return;
    socket_retry_at_=millis();
    if(!open_socket_()) return;
    owner_->clients_[0]->sed_udp_ready(ready());
    // Always permit a fresh descriptor exchange after recommissioning. This
    // does not depend on the IP changing or on the HA schema cache being empty.
    if(ready()) request_schema();
    ESP_LOGI(TAG,"UDP listener restored on port %u",port_);
    // Let sed_tick() finish interrupted actions before building a new snapshot.
    return;
  }
  receive_();
  auto *client=owner_->clients_[0].get();
  if(schema_requested_ && client->sed_flush_ready() && !schema_building_) {
    if(!tx_.empty() && paused_tx_.empty()) {
      paused_tx_=std::move(tx_); tx_.clear(); paused_control_=std::move(tx_control_); paused_group_=tx_group_;
      paused_ack_timeout_=tx_ack_timeout_; paused_ack_resend_=tx_ack_resend_;
      paused_peer_=tx_peer_; paused_peer_len_=tx_peer_len_;
    }
    schema_requested_=false; schema_building_=true;
    schema_peer_=requested_schema_peer_; schema_peer_len_=requested_schema_peer_len_;
    ESP_LOGI(TAG,"Building entity descriptors for UDP onboarding/sync");
    client->on_device_info_request(); client->on_subscribe_homeassistant_services_request();
    client->on_list_entities_request();
  }
  client->loop();
  if(schema_building_ && tx_.empty() && client->sed_flush_ready()) flush();
  if(!tx_.empty()) {
    const size_t parts=(tx_.size()+CHUNK-1)/CHUNK;
    if(tx_part_<parts) {
      // Only emission stalls use an internal deadline. ACK timing starts when
      // the final fragment has actually reached the socket, on every round.
      if(millis()-tx_started_>=5000U) fail_("send_stalled");
      else transmit_();
    } else if(millis()-last_send_>=tx_ack_timeout_) {
      if(attempts_<=tx_ack_resend_) {
        tx_part_=0; tx_started_=millis(); transmit_();
      } else fail_("ack_timeout");
    }
  }
  if(!schema_building_ && tx_.empty() && !owner_->sed_batch_done_ &&
      !owner_->sed_auto_pending() && !staging_.empty() && millis()-staged_at_>=20) flush();
}
APIError APIUDPFrameHelper::write_protobuf_packet(uint16_t type,ProtoWriteBuffer buffer) {
  const auto *data=buffer.get_buffer();
  // Native encoding reserves the record header. append() writes the actual
  // wire header, so skip that padding even when the protobuf body is empty.
  if(data->size()<UDP_PROTO_HEADER_SIZE) return APIError::OUT_OF_MEMORY;
  return owner_->udp_.append(type,data->data()+UDP_PROTO_HEADER_SIZE,data->size()-UDP_PROTO_HEADER_SIZE)
      ?APIError::OK:APIError::OUT_OF_MEMORY;
}
APIError APIUDPFrameHelper::write_protobuf_messages(ProtoWriteBuffer buffer,std::span<const MessageInfo> messages) {
  const auto *data=buffer.get_buffer();
  for(const auto &message:messages)
    if(!owner_->udp_.append(message.message_type,data->data()+message.offset+message.header_size,message.payload_size))
      return APIError::OUT_OF_MEMORY;
  return APIError::OK;
}
}  // namespace esphome::thread_sed_api
#endif
