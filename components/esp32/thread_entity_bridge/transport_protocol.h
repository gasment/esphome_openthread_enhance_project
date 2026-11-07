#pragma once
// Transport protocol v2. This header is byte-identical on ESP32 and nRF52.
#include <algorithm>
#include <array>
#include <cstdint>
#include <utility>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include "cJSON.h"
#include "esphome/core/helpers.h"
#include "openthread/thread.h"
#include "openthread/link.h"
#include "openthread/ip6.h"
#ifdef USE_ESP32
#include "nvs.h"
#else
#include "openthread/platform/settings.h"
#endif

namespace esphome::thread_transport {
inline constexpr const char *PUBLIC_GROUP = "ff13::8000:8562";
inline constexpr uint16_t PUBLIC_PORT = 18512;
inline constexpr unsigned VERSION = 2;
inline const cJSON *field(const cJSON *j, const char *key) { return cJSON_GetObjectItemCaseSensitive(j,key); }
inline std::string text(const cJSON *j, const char *key) {
  const auto *v=field(j,key); return cJSON_IsString(v)&&v->valuestring?v->valuestring:"";
}
inline bool uint32(const cJSON *v) {
  return cJSON_IsNumber(v)&&std::isfinite(v->valuedouble)&&v->valuedouble>=0&&v->valuedouble<=UINT32_MAX&&std::floor(v->valuedouble)==v->valuedouble;
}
inline std::string encode(const cJSON *j) { char *s=cJSON_PrintUnformatted(j);std::string out=s?s:"";cJSON_free(s);return out; }
inline bool hex_id(const std::string &s,size_t length) {
  return s.size()==length&&s.find_first_not_of("0123456789abcdef")==std::string::npos;
}
inline std::string nonce() {
  uint8_t bytes[16]; if(!random_bytes(bytes,sizeof(bytes)))return "";
  char out[33]; for(unsigned i=0;i<16;i++)snprintf(out+2*i,3,"%02x",bytes[i]);return out;
}
inline std::string network(otInstance *ot) {
  char out[17];const auto *pan=otThreadGetExtendedPanId(ot);
  for(unsigned i=0;i<8;i++)snprintf(out+2*i,3,"%02x",pan->m8[i]);return out;
}
inline std::string device(otInstance *ot) {
  otExtAddress address{};otLinkGetFactoryAssignedIeeeEui64(ot,&address);
  char out[17];for(unsigned i=0;i<8;i++)snprintf(out+2*i,3,"%02x",address.m8[i]);return out;
}
// Portable SHA-256, avoiding platform crypto configuration differences.
inline uint32_t rotr(uint32_t x,unsigned n){return (x>>n)|(x<<(32-n));}
inline std::string sha256(const std::string &input) {
  static constexpr uint32_t k[]={
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
  uint32_t h[]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
  const size_t blocks=(input.size()+9+63)/64;
  const uint64_t bits=uint64_t(input.size())*8;
  for(size_t block=0;block<blocks;block++) {
    uint32_t w[64]{};
    for(unsigned i=0;i<64;i++) {
      const size_t pos=block*64+i;uint8_t b=0;
      if(pos<input.size())b=uint8_t(input[pos]);else if(pos==input.size())b=0x80;
      if(pos>=blocks*64-8)b=uint8_t(bits>>(8*(blocks*64-1-pos)));
      w[i/4]|=uint32_t(b)<<(24-8*(i%4));
    }
    for(unsigned i=16;i<64;i++)w[i]=w[i-16]+(rotr(w[i-15],7)^rotr(w[i-15],18)^(w[i-15]>>3))+w[i-7]+(rotr(w[i-2],17)^rotr(w[i-2],19)^(w[i-2]>>10));
    uint32_t a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],v=h[7];
    for(unsigned i=0;i<64;i++) {
      uint32_t t=v+(rotr(e,6)^rotr(e,11)^rotr(e,25))+((e&f)^(~e&g))+k[i]+w[i];
      uint32_t u=(rotr(a,2)^rotr(a,13)^rotr(a,22))+((a&b)^(a&c)^(b&c));
      v=g;g=f;f=e;e=d+t;d=c;c=b;b=a;a=t+u;
    }
    h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=v;
  }
  char out[65];for(unsigned i=0;i<8;i++)snprintf(out+i*8,9,"%08x",unsigned(h[i]));return out;
}
inline std::string canonical(const cJSON *j,unsigned depth=0) {
  if(!j||depth>16)return "";
  if(cJSON_IsObject(j)) {
    std::vector<const cJSON *> fields;for(auto *p=j->child;p;p=p->next)fields.push_back(p);
    std::sort(fields.begin(),fields.end(),[](auto *a,auto *b){return std::strcmp(a->string,b->string)<0;});
    std::string out="{";const char *previous=nullptr;
    for(auto *p:fields) {
      if(previous&&std::strcmp(previous,p->string)==0)return "";
      if(previous)out+=',';auto *key=cJSON_CreateString(p->string);out+=encode(key);cJSON_Delete(key);
      auto value=canonical(p,depth+1);if(value.empty())return "";out+=':';out+=value;previous=p->string;
    }return out+'}';
  }
  if(cJSON_IsArray(j)) {
    std::string out="[";for(auto *p=j->child;p;p=p->next){auto v=canonical(p,depth+1);if(v.empty())return "";if(p!=j->child)out+=',';out+=v;}return out+']';
  }
  if(cJSON_IsNumber(j)){if(!std::isfinite(j->valuedouble))return "";char b[32];snprintf(b,sizeof(b),"%.17g",j->valuedouble==0?0:j->valuedouble);return b;}
  return encode(j);
}
inline std::string manifest_hash(const std::string &name,const std::vector<std::string> &descriptors) {
  std::vector<std::pair<std::string,std::string>> entries;
  for(auto &wire:descriptors){auto *j=cJSON_Parse(wire.c_str());auto id=text(j,"id"),value=canonical(j);cJSON_Delete(j);if(id.empty()||value.empty())return "";entries.emplace_back(id,value);}
  std::sort(entries.begin(),entries.end());auto *n=cJSON_CreateString(name.c_str());std::string wire=encode(n)+"\n";cJSON_Delete(n);
  std::string last;for(auto &entry:entries){if(entry.first==last)return "";last=entry.first;wire+=entry.second+'\n';}return sha256(wire);
}
struct BindingData {
  uint32_t magic{0x32544742},generation{0};
  char network[17]{},device[17]{},node[33]{},owner[33]{},token[33]{};
  uint8_t manual_required{0};
};
class Binding {
 public:
  BindingData data{};
  bool loaded{false};
  bool store(otInstance *ot,const BindingData &next) {
    bool ok=false;
#ifdef USE_ESP32
    nvs_handle_t nvs;
    if(nvs_open("thread_bind_v2",NVS_READWRITE,&nvs)==ESP_OK){ok=nvs_set_blob(nvs,"binding",&next,sizeof(next))==ESP_OK&&nvs_commit(nvs)==ESP_OK;nvs_close(nvs);}
#else
    // The Zephyr backend preserves all 16 key bits. 0x8001 belongs to Joiner rollback.
    ok=otPlatSettingsSet(ot,0x8002,reinterpret_cast<const uint8_t *>(&next),sizeof(next))==OT_ERROR_NONE;
#endif
    if(ok)data=next;return ok;
  }
  bool refresh(otInstance *ot,const std::string &node,bool &changed) {
    changed=false;if(!ot||otThreadGetDeviceRole(ot)<OT_DEVICE_ROLE_CHILD)return false;
    if(!loaded) {
      BindingData saved{};bool found=false,missing=false;
#ifdef USE_ESP32
      nvs_handle_t nvs;auto err=nvs_open("thread_bind_v2",NVS_READONLY,&nvs);
      if(err==ESP_ERR_NVS_NOT_FOUND)missing=true;
      else if(err==ESP_OK){size_t size=sizeof(saved);err=nvs_get_blob(nvs,"binding",&saved,&size);found=err==ESP_OK&&size==sizeof(saved);missing=err==ESP_ERR_NVS_NOT_FOUND;nvs_close(nvs);}
#else
      uint16_t size=sizeof(saved);auto err=otPlatSettingsGet(ot,0x8002,0,reinterpret_cast<uint8_t *>(&saved),&size);
      found=err==OT_ERROR_NONE&&size==sizeof(saved);missing=err==OT_ERROR_NOT_FOUND;
#endif
      if(!found&&!missing)return false; // Fail closed on unreadable/corrupt storage.
      if(found){if(saved.magic!=data.magic||saved.network[16]||saved.device[16]||saved.node[32]||saved.owner[32]||saved.token[32])return false;data=saved;}
      loaded=true;
    }
    const auto net=network(ot),dev=device(ot);
    if(net!=data.network||dev!=data.device||node!=data.node) {
      BindingData next{};next.generation=data.generation;
      if(next.generation==UINT32_MAX)return false;++next.generation;
      snprintf(next.network,sizeof(next.network),"%s",net.c_str());snprintf(next.device,sizeof(next.device),"%s",dev.c_str());snprintf(next.node,sizeof(next.node),"%s",node.c_str());
      if(!store(ot,next))return false;changed=true;
    }return true;
  }
  bool bind(otInstance *ot,const std::string &owner,const std::string &token) {
    if(owner.empty()||owner.size()>32||!hex_id(token,32))return false;
    if(data.owner[0])return owner==data.owner&&token==data.token;
    if(data.generation==UINT32_MAX)return false;auto next=data;++next.generation;
    snprintf(next.owner,sizeof(next.owner),"%s",owner.c_str());snprintf(next.token,sizeof(next.token),"%s",token.c_str());next.manual_required=0;return store(ot,next);
  }
  bool revoke(otInstance *ot,const std::string &token) {
    if(token!=data.token||!data.owner[0]||data.generation==UINT32_MAX)return false;
    auto next=data;++next.generation;next.owner[0]=next.token[0]=0;next.manual_required=1;return store(ot,next);
  }
  void describe(cJSON *j) const {
    cJSON_AddStringToObject(j,"network",data.network);cJSON_AddStringToObject(j,"device",data.device);
    cJSON_AddStringToObject(j,"owner",data.owner);cJSON_AddStringToObject(j,"binding_id",data.token);
    cJSON_AddNumberToObject(j,"generation",data.generation);cJSON_AddBoolToObject(j,"manual_required",data.manual_required);
  }
};
}  // namespace esphome::thread_transport
