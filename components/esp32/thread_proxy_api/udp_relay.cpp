#include "udp_relay.h"
#include "thread_proxy_api.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "esp_openthread.h"
#include "esp_openthread_lock.h"
#include "openthread/thread.h"
#include <algorithm>
#include <cstdio>

namespace esphome::thread_proxy_api {
static const char *const TAG = "thread_proxy_api.udp";
static uint16_t u16(const uint8_t *p) { return uint16_t(p[0]) << 8 | p[1]; }
static uint32_t u32(const uint8_t *p) { return uint32_t(p[0])<<24 | uint32_t(p[1])<<16 | uint32_t(p[2])<<8 | p[3]; }
static void put32(uint8_t *p, uint32_t n) { for (int i=3; i>=0; --i) { p[i]=n; n>>=8; } }
static bool identity(const uint8_t *a, const uint8_t *b) {
  // Retries have new sequence numbers; group identity does not.
  return a[4]==b[4] && a[5]==b[5] && !memcmp(a+6,b+6,14) && !memcmp(a+24,b+24,4) && !memcmp(a+32,b+32,16);
}
static uint64_t digest(const uint8_t *p, size_t n) {
  uint64_t h=14695981039346656037ULL;
  for (size_t i=0;i<n;++i) h=(h^p[i])*1099511628211ULL;
  return h;
}
#ifdef USE_THREAD_PROXY_UDP_ENCRYPTION
void UdpRelay::set_key(const std::array<uint8_t,32> &key) {
  mbedtls_gcm_init(&crypto_);
  encrypted_=mbedtls_gcm_setkey(&crypto_,MBEDTLS_CIPHER_ID_AES,key.data(),256)==0;
  if (!encrypted_) owner_->mark_failed();
}
#endif
bool UdpRelay::request_schema(const std::string &mac) {
  if(!requests_ || mac.size()!=12) return false;
  std::array<uint8_t,6> bytes{};
  for(size_t i=0;i<12;++i) {
    const char c=mac[i];
    const int value=c>='0' && c<='9'?c-'0':c>='a' && c<='f'?c-'a'+10:-1;
    if(value<0) return false;
    bytes[i/2]|=value<<(i%2?0:4);
  }
  return xQueueSend(requests_,bytes.data(),0)==pdTRUE;
}
bool UdpRelay::setup() {
  if (!random_bytes(boot_.data(),boot_.size())) return false;
  pending_acks_.reserve(16);
  incoming_=xQueueCreate(8,sizeof(Datagram)); requests_=xQueueCreate(8,6); mutex_=xSemaphoreCreateMutex();
  if (!incoming_ || !requests_ || !mutex_) {
    if(incoming_) vQueueDelete(incoming_);
    if(requests_) vQueueDelete(requests_);
    if(mutex_) vSemaphoreDelete(mutex_);
    return false;
  }
  TaskHandle_t task=nullptr;
  if(xTaskCreate(task_,"proxy_udp",6144,this,1,&task)!=pdPASS) {
    vQueueDelete(incoming_); vQueueDelete(requests_); vSemaphoreDelete(mutex_); return false;
  }
  return true;
}
void UdpRelay::receive_(void *arg, otMessage *message, const otMessageInfo *peer) {
  auto *self=static_cast<UdpRelay *>(arg);
  const uint16_t size=otMessageGetLength(message)-otMessageGetOffset(message);
  if(self->stopping_ || size<48 || size>832) return;
  Datagram packet{}; packet.peer=*peer; packet.size=size; packet.received_at=millis();
  if(otMessageRead(message,otMessageGetOffset(message),packet.data,size)==size &&
      xQueueSend(self->incoming_,&packet,0)!=pdTRUE) self->receive_drops_.fetch_add(1);
}
void UdpRelay::task_(void *arg) { static_cast<UdpRelay *>(arg)->run_(); vTaskDelete(nullptr); }
void UdpRelay::run_() {
  Datagram packet{};
  uint32_t reported_drops=0, last_diagnostics=millis();
  while(!stopping_) {
    if(!owner_->parent_->is_thread_ready()) ready_=false;
    else if(esp_openthread_lock_acquire(pdMS_TO_TICKS(10))) {
      auto *instance=esp_openthread_get_instance();
      if(instance && !opened_) {
        otSockAddr local{}; local.mPort=6064;
        if(otUdpOpen(instance,&socket_,receive_,this)==OT_ERROR_NONE) {
          if(otUdpBind(instance,&socket_,&local,OT_NETIF_THREAD_INTERNAL)==OT_ERROR_NONE) opened_=true;
          else otUdpClose(instance,&socket_);
        }
      }
      ready_=opened_ && instance && otThreadGetDeviceRole(instance)>=OT_DEVICE_ROLE_CHILD;
      esp_openthread_lock_release();
    } // Lock contention does not mean Thread detached. Preserve the last verified role.
    if(xQueueReceive(incoming_,&packet,pdMS_TO_TICKS(10))==pdTRUE) process_(packet);
    retry_acks_();
    uint32_t now=millis();
    if(now-last_diagnostics>=5000) {
      const uint32_t drops=receive_drops_.load();
      if(drops!=reported_drops) ESP_LOGW(TAG,"UDP receive FIFO dropped %lu datagrams (total=%lu)",static_cast<unsigned long>(drops-reported_drops),static_cast<unsigned long>(drops));
      reported_drops=drops; last_diagnostics=now;
    }
    assemblies_.erase(std::remove_if(assemblies_.begin(),assemblies_.end(),[now](const auto &a){return now-a.touched>10000;}),assemblies_.end());
    while(!receipts_.empty() && now-receipts_.front().at>120000) receipts_.pop_front();
    if(ready_) schema_requests_();
  }
  ready_=false;
  // Never delete a worker while it holds the Thread lock.
  if(opened_ && esp_openthread_lock_acquire(pdMS_TO_TICKS(100))) {
    auto *instance=esp_openthread_get_instance();
    if(instance) otUdpClose(instance,&socket_);
    esp_openthread_lock_release();
  }
}
UdpRelay::Node *UdpRelay::node_(const otIp6Address &address, uint16_t port) {
  for(auto &node:nodes_) if(!memcmp(&node.address,&address,16)) return &node;
  if(nodes_.size()>=64) return nullptr;
  nodes_.push_back(Node{}); auto &node=nodes_.back(); node.address=address; node.port=port;
  node_count_=nodes_.size(); return &node;
}
void UdpRelay::process_(Datagram &packet) {
  const uint32_t queue_wait=millis()-packet.received_at;
  auto *h=packet.data; size_t size=packet.size-48;
  if(memcmp(h,"TSD2",4) || (h[4]!=2 && h[4]!=6 && h[4]!=7 && h[4]!=8) || (h[5]&~3) || bool(h[5]&1)!=encrypted_) return;
  const unsigned part=u16(h+28), parts=u16(h+30);
  if(!parts || parts>43 || part>=parts) return;
  #ifdef USE_THREAD_PROXY_UDP_ENCRYPTION
  if(encrypted_) {
    if(size<16) return;
    size-=16; if(size>768) return; uint8_t nonce[12]; memcpy(nonce,h+12,12);
    uint8_t clear[768]; // Hardware GCM backends need non-overlapping buffers.
    if(mbedtls_gcm_auth_decrypt(&crypto_,size,nonce,12,h,48,h+48+size,16,h+48,clear)!=0) return;
    if(size) memcpy(h+48,clear,size);
  }
  #endif
  if(size>768 || (part+1<parts && size!=768) || size_t(part)*768+size>32768) return;
  // Remember peers only after authentication/structural validation.
  Node *known=nullptr;
  for(auto &candidate:nodes_) if(!memcmp(candidate.mac.data(),h+6,6)) { known=&candidate; break; }
  if(!known) known=node_(packet.peer.mPeerAddr,packet.peer.mPeerPort);
  if(auto *node=known) {
    node->address=packet.peer.mPeerAddr; node->port=packet.peer.mPeerPort;
    memcpy(node->mac.data(),h+6,6); node->last_seen=millis();
    if(h[4]==2 && !(h[5]&2) && memcmp(node->schema.data(),h+32,16)) {
      node->schema.fill(0); node->last_probe=0; // A firmware/schema change needs fresh descriptors even during frequent reporting.
    }
  }
  if(h[4]==8) {
    if(parts!=1 || size || h[5]&2) return;
    xSemaphoreTake(mutex_,portMAX_DELAY);
    const bool full=queue_.size()>=max_groups_ || bytes_>=max_bytes_;
    xSemaphoreGive(mutex_);
    if(full) return; // Let a BR with queue capacity answer discovery first.
    uint8_t response[48]; memcpy(response,h,48); memset(response+32,0,16);
    for(const auto &node:nodes_) if(!memcmp(node.mac.data(),h+6,6)) { memcpy(response+32,node.schema.data(),16); break; }
    // Reply from this BR's unicast Thread address, never from multicast ff03::1.
    send_(1,response,h+12,8,packet.peer); return;
  }
  if(h[4]!=2) {
    if(parts!=1 || (h[4]==6 ? size!=4 : size!=0)) return;
    accept_(h,h+48,size); return; // SLEEP/HEARTBEAT never wait for a node ACK.
  }
  if(parts==1) {
    if(accept_(h,h+48,size)) {
      acknowledge_node_(h,packet.peer);
      ESP_LOGD(TAG,"DATA accepted group=%lu bytes=%u queue_wait=%lu ms pending=%u",static_cast<unsigned long>(u32(h+24)),unsigned(size),static_cast<unsigned long>(queue_wait),unsigned(queued_.load()));
    }
    return;
  }
  auto it=std::find_if(assemblies_.begin(),assemblies_.end(),[&](const auto &a){return identity(a.header.data(),h) &&
    !memcmp(&a.peer.mPeerAddr,&packet.peer.mPeerAddr,16) && !memcmp(&a.peer.mSockAddr,&packet.peer.mSockAddr,16) && a.peer.mPeerPort==packet.peer.mPeerPort;});
  if(it==assemblies_.end()) {
    if(assemblies_.size()>=4) return;
    Assembly a; memcpy(a.header.data(),h,48); a.peer=packet.peer;
    if(!a.body.resize(std::min(size_t(parts)*768,size_t(32768)))) return;
    assemblies_.push_back(std::move(a)); it=assemblies_.end()-1;
  }
  if(u16(it->header.data()+30)!=parts) return;
  const uint64_t bit=uint64_t{1}<<part;
  if(it->mask&bit) {
    if((part+1==parts && it->final_size!=size) || memcmp(it->body.data()+part*768,h+48,size)) return;
  } else {
    memcpy(it->body.data()+part*768,h+48,size); it->mask|=bit;
    if(part+1==parts) it->final_size=size;
  }
  it->touched=millis();
  if(it->mask!=(uint64_t{1}<<parts)-1) return;
  if(accept_(it->header.data(),it->body.data(),(parts-1)*768+it->final_size)) acknowledge_node_(h,packet.peer);
  assemblies_.erase(it);
}
bool UdpRelay::accept_(const uint8_t *h, const uint8_t *body, size_t size) {
  if(h[4]==2) {
    size_t offset=0;
    while(offset<size) {
      if(size-offset<4 || !u16(body+offset) || u16(body+offset+2)>size-offset-4) return false;
      offset+=4+u16(body+offset+2);
    }
  }
  const uint64_t hash=digest(body,size);
  for(const auto &r:receipts_) if(identity(r.header.data(),h)) return r.hash==hash;
  // A pending group must remain ACKable even after the short receipt LRU has
  // expired/evicted its entry (e.g. HA offline and a queue with >64 groups).
  xSemaphoreTake(mutex_,portMAX_DELAY);
  for(const auto &pending:queue_) {
    if(!identity(pending->header.data(),h)) continue;
    const bool same=pending->body.size()==size && (!size || !memcmp(pending->body.data(),body,size));
    xSemaphoreGive(mutex_); return same;
  }
  xSemaphoreGive(mutex_);
  auto group=std::make_shared<Group>();
  if(!group->body.resize(size)) return false;
  memcpy(group->header.data(),h,48); if(size) memcpy(group->body.data(),body,size);
  char token[34];
  std::snprintf(token,sizeof(token),"%02x%02x%02x%02x%02x%02x%02x%02x-%08lx",boot_[0],boot_[1],boot_[2],boot_[3],boot_[4],boot_[5],boot_[6],boot_[7],static_cast<unsigned long>(++delivery_));
  group->delivery=token;
  xSemaphoreTake(mutex_,portMAX_DELAY);
  if(queue_.size()>=max_groups_ || size>max_bytes_-bytes_) { xSemaphoreGive(mutex_); ESP_LOGW(TAG,"Relay queue full; withholding ACK"); return false; }
  queue_.push_back(group); bytes_+=size; queued_=queue_.size();
  xSemaphoreGive(mutex_);
  Receipt receipt{}; memcpy(receipt.header.data(),h,48); receipt.hash=hash; receipt.at=millis();
  receipts_.push_back(receipt); if(receipts_.size()>64) receipts_.pop_front();
  if((h[5]&2) && h[4]==2) for(auto &n:nodes_) if(!memcmp(n.mac.data(),h+6,6)) memcpy(n.schema.data(),h+32,16);
  ESP_LOGV(TAG,"Queued node group=%lu pending=%u bytes=%u",static_cast<unsigned long>(u32(h+24)),unsigned(queued_.load()),unsigned(size));
  return true;
}
std::shared_ptr<const UdpRelay::Group> UdpRelay::front() {
  if(!mutex_) return {};
  xSemaphoreTake(mutex_,portMAX_DELAY);
  std::shared_ptr<const Group> result=queue_.empty()?nullptr:queue_.front();
  xSemaphoreGive(mutex_); return result;
}
bool UdpRelay::acknowledge(const std::string &delivery) {
  if(!mutex_) return false;
  xSemaphoreTake(mutex_,portMAX_DELAY);
  bool matches=!queue_.empty() && queue_.front()->delivery==delivery;
  if(matches) { bytes_-=queue_.front()->body.size(); queue_.pop_front(); queued_=queue_.size(); }
  xSemaphoreGive(mutex_); return matches;
}
bool UdpRelay::send_(uint8_t kind, const uint8_t *header, const uint8_t *body, size_t size, const otMessageInfo &peer) {
  if(size>8) return false;
  uint8_t packet[80]{};
  memcpy(packet,"TSD2",4); packet[4]=kind; packet[5]=encrypted_?1:0;
  memcpy(packet+6,header+6,6); memcpy(packet+12,boot_.data(),8);
  if(++sequence_==0 && !random_bytes(boot_.data(),8)) { stopping_=true; owner_->mark_failed(); return false; }
  memcpy(packet+12,boot_.data(),8); put32(packet+20,sequence_);
  memcpy(packet+24,header+24,4); packet[31]=1; memcpy(packet+32,header+32,16);
  if(size) memcpy(packet+48,body,size);
  size_t length=48+size;
  #ifdef USE_THREAD_PROXY_UDP_ENCRYPTION
  if(encrypted_) {
    uint8_t clear[8]{};
    if(size) memcpy(clear,body,size);
    if(mbedtls_gcm_crypt_and_tag(&crypto_,MBEDTLS_GCM_ENCRYPT,size,packet+12,12,packet,48,clear,packet+48,16,packet+length)!=0) return false;
    length+=16;
  }
  #endif
  if(!ready_ || !esp_openthread_lock_acquire(pdMS_TO_TICKS(10))) return false;
  auto *instance=esp_openthread_get_instance();
  bool sent=false;
  if(instance && opened_) {
    otMessageInfo info{}; info.mPeerAddr=peer.mPeerAddr; info.mPeerPort=peer.mPeerPort; info.mSockAddr=peer.mSockAddr; info.mSockPort=6064;
    if(kind!=3) info.mSockAddr=*otThreadGetMeshLocalEid(instance);
    auto *message=otUdpNewMessage(instance,nullptr);
    if(message) {
      sent=otMessageAppend(message,packet,length)==OT_ERROR_NONE && otUdpSend(instance,&socket_,message,&info)==OT_ERROR_NONE;
      if(!sent) otMessageFree(message);
    }
  }
  esp_openthread_lock_release(); return sent;
}
void UdpRelay::acknowledge_node_(const uint8_t *header,const otMessageInfo &peer) {
  auto same=[&](const PendingAck &ack) {
    return identity(ack.header.data(),header) && ack.peer.mPeerPort==peer.mPeerPort &&
      !memcmp(&ack.peer.mPeerAddr,&peer.mPeerAddr,16) && !memcmp(&ack.peer.mSockAddr,&peer.mSockAddr,16);
  };
  auto existing=std::find_if(pending_acks_.begin(),pending_acks_.end(),same);
  if(send_(3,header,header+12,8,peer)) {
    if(existing!=pending_acks_.end()) pending_acks_.erase(existing);
    ESP_LOGD(TAG,"ACK submitted group=%lu",static_cast<unsigned long>(u32(header+24)));
    return;
  }
  if(existing!=pending_acks_.end()) { existing->started=millis(); existing->attempted=millis(); return; }
  if(pending_acks_.size()>=16) {
    ESP_LOGW(TAG,"ACK retry queue full group=%lu; node retry required",static_cast<unsigned long>(u32(header+24))); return;
  }
  PendingAck ack; memcpy(ack.header.data(),header,48); ack.peer=peer;
  ack.started=ack.attempted=millis(); ack.attempts=1;
  pending_acks_.push_back(ack);
  ESP_LOGD(TAG,"ACK deferred group=%lu; OT lock/socket submission unavailable",static_cast<unsigned long>(u32(header+24)));
}
void UdpRelay::retry_acks_() {
  unsigned work=0;
  for(auto it=pending_acks_.begin();it!=pending_acks_.end();) {
    const uint32_t now=millis();
    if(now-it->started>=400) {
      ESP_LOGW(TAG,"ACK submission expired group=%lu attempts=%u; accepted data retained",static_cast<unsigned long>(u32(it->header.data()+24)),unsigned(it->attempts));
      it=pending_acks_.erase(it); continue;
    }
    if(work>=4 || now-it->attempted<10) { ++it; continue; }
    ++work; ++it->attempts; it->attempted=now;
    if(send_(3,it->header.data(),it->header.data()+12,8,it->peer)) {
      ESP_LOGD(TAG,"ACK submitted group=%lu after %u attempts, %lu ms",static_cast<unsigned long>(u32(it->header.data()+24)),unsigned(it->attempts),static_cast<unsigned long>(millis()-it->started));
      it=pending_acks_.erase(it);
    } else ++it;
  }
}
void UdpRelay::schema_requests_() {
  std::array<uint8_t,6> mac{};
  for(unsigned work=0;work<2 && xQueueReceive(requests_,mac.data(),0)==pdTRUE;++work) {
    Node *selected=nullptr; const uint32_t now=millis();
    for(auto &node:nodes_) if(node.mac==mac && (!selected || now-node.last_seen<now-selected->last_seen)) selected=&node;
    if(!selected) continue;
    uint8_t header[48]{}; memcpy(header+6,mac.data(),6);
    otMessageInfo peer{}; peer.mPeerAddr=selected->address; peer.mPeerPort=selected->port;
    send_(4,header,nullptr,0,peer);
  }
}
}  // namespace esphome::thread_proxy_api
