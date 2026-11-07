#include "thread_gateway.h"
#ifdef USE_THREAD_GATEWAY
#include "esphome/core/log.h"
#include "esp_openthread.h"
#include "esp_openthread_lock.h"
#include "nvs.h"
#include <set>

namespace esphome::thread_gateway {
namespace {
using thread_transport::text;
using thread_transport::field;
using thread_transport::encode;
using thread_transport::hex_id;
constexpr size_t MAX_BINDINGS=256, MAX_CANDIDATES=128, MAX_NODES=128;
bool identifier(const std::string &s){return !s.empty()&&s.size()<=32&&s.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")==std::string::npos;}
cJSON *error(int &status,int code,const std::string &message){status=code;auto *j=cJSON_CreateObject();cJSON_AddStringToObject(j,"error",message.c_str());return j;}
}
bool ThreadGateway::revoked_(const std::string &token) const {
  if(token.empty())return false;
  for(const auto &r:ownership_)if(r.token==token&&r.state=="revoked")return true;
  return false;
}
const ThreadGateway::Ownership *ThreadGateway::owner_(const std::string &node,const std::string &scope) const {
  const Ownership *best=nullptr;
  for(const auto &r:ownership_)if(r.network==(scope.empty()?network_:scope)&&r.node==node&&r.state!="pending"&&r.state!="revoked"&&!revoked_(r.token)) {
    if(!best||r.generation>best->generation)best=&r;
    else if(r.generation==best->generation&&r.token!=best->token)return nullptr;
  }
  return best&&best->state=="active"?best:nullptr;
}
bool ThreadGateway::authorized_(const Node &node) const {
  const auto *r=owner_(node.id);return ledger_ready_&&node.network==network_&&r&&r->owner==gateway_id_&&!revoked_(r->token);
}
ThreadGateway::Candidate *ThreadGateway::candidate_(const std::string &node) {
  for (auto &c : candidates_) {
    if (c.node == node) {
      return &c;
    }
  }
  return nullptr;
}
void ThreadGateway::load_ownership_() {
  nvs_handle_t nvs;auto err=nvs_open("thread_bind_v2",NVS_READONLY,&nvs);
  if(err==ESP_ERR_NVS_NOT_FOUND){ledger_ready_=true;return;}
  if(err!=ESP_OK){ledger_error_=esp_err_to_name(err);return;}
  size_t length=0;err=nvs_get_blob(nvs,"ledger",nullptr,&length);
  if(err==ESP_ERR_NVS_NOT_FOUND){nvs_close(nvs);ledger_ready_=true;return;}
  if(err!=ESP_OK||length<2||length>131072){nvs_close(nvs);ledger_error_="Invalid binding ledger size";return;}
  std::vector<char> bytes(length);err=nvs_get_blob(nvs,"ledger",bytes.data(),&length);nvs_close(nvs);
  if(err!=ESP_OK||bytes.back()!=0){ledger_error_="Cannot read binding ledger";return;}
  auto *root=cJSON_Parse(bytes.data());const auto *records=field(root,"records");
  bool valid=thread_transport::uint32(field(root,"version"))&&field(root,"version")->valueint==2&&cJSON_IsArray(records)&&cJSON_GetArraySize(records)<=int(MAX_BINDINGS);
  std::vector<Ownership> loaded;
  const cJSON *j;if(valid)cJSON_ArrayForEach(j,records) {
    Ownership r;r.network=text(j,"network");r.device=text(j,"device");r.node=text(j,"node");r.name=text(j,"name");r.node_type=text(j,"node_type");r.owner=text(j,"owner");r.token=text(j,"binding_id");r.state=text(j,"state");
    if(!hex_id(r.network,16)||!hex_id(r.device,16)||!identifier(r.node)||r.name.size()>96||
       (r.node_type!="sed"&&r.node_type!="reed")||!thread_transport::uint32(field(j,"generation"))||
       (r.state!="active"&&r.state!="pending"&&r.state!="revoked"&&r.state!="unbound")||
       (r.state!="unbound"&&(!identifier(r.owner)||!hex_id(r.token,32)))){valid=false;break;}
    r.generation=field(j,"generation")->valuedouble;loaded.push_back(std::move(r));
  }
  cJSON_Delete(root);if(!valid){ledger_error_="Corrupt binding ledger; authorization disabled";return;}
  ownership_=std::move(loaded);ledger_ready_=true;
}
bool ThreadGateway::save_ownership_() {
  if(!ledger_ready_){ledger_error_="Binding ledger unavailable";return false;}
  auto *root=cJSON_CreateObject();cJSON_AddNumberToObject(root,"version",2);auto *records=cJSON_AddArrayToObject(root,"records");
  for(const auto &r:ownership_) {
    auto *j=cJSON_CreateObject();cJSON_AddItemToArray(records,j);
    for(const auto &p:std::initializer_list<std::pair<const char *,std::string>>{{"network",r.network},{"device",r.device},{"node",r.node},{"name",r.name},{"node_type",r.node_type},{"owner",r.owner},{"binding_id",r.token},{"state",r.state}})cJSON_AddStringToObject(j,p.first,p.second.c_str());
    cJSON_AddNumberToObject(j,"generation",r.generation);
  }
  auto wire=encode(root);cJSON_Delete(root);if(wire.empty()||wire.size()>131071){ledger_error_="Binding ledger exceeds storage limit";return false;}
  nvs_handle_t nvs;auto err=nvs_open("thread_bind_v2",NVS_READWRITE,&nvs);
  if(err==ESP_OK){err=nvs_set_blob(nvs,"ledger",wire.c_str(),wire.size()+1);if(err==ESP_OK)err=nvs_commit(nvs);nvs_close(nvs);}
  if(err!=ESP_OK){ledger_error_=esp_err_to_name(err);return false;}ledger_error_.clear();return true;
}
bool ThreadGateway::merge_ownership_(const Ownership &record) {
  if(!ledger_ready_)return false;
  for(const auto &r:ownership_)if(r.network==record.network&&r.device==record.device&&r.state!="pending"&&r.generation>record.generation&&record.state!="revoked")return true;
  auto previous=ownership_;
  // Latest node-confirmed state is a watermark; older snapshots cannot return.
  if(record.state=="active"||record.state=="unbound")
    ownership_.erase(std::remove_if(ownership_.begin(),ownership_.end(),[&](const Ownership &r){return r.network==record.network&&r.device==record.device&&r.state!="revoked"&&r.generation<record.generation;}),ownership_.end());
  // Keep every revocation tombstone. Never recycle it based on elapsed time.
  auto same=std::find_if(ownership_.begin(),ownership_.end(),[&](const Ownership &r){return r.network==record.network&&r.device==record.device&&r.token==record.token;});
  if(same!=ownership_.end()) {
    if(same->state=="revoked"||same->generation>record.generation)return true;
    if(same->generation==record.generation&&same->state==record.state)return true;
    *same=record;
  }else{
    if(ownership_.size()>=MAX_BINDINGS){ledger_error_="Binding ledger full; tombstones were retained";return false;}
    ownership_.push_back(record);
  }
  if(record.state=="active"||record.state=="revoked")
    ownership_.erase(std::remove_if(ownership_.begin(),ownership_.end(),[&](const Ownership &r){return r.network==record.network&&r.device==record.device&&r.state=="pending"&&r.token!=record.token;}),ownership_.end());
  if(!save_ownership_()){ownership_=std::move(previous);return false;}
  if(record.state=="active"||record.state=="revoked")for(auto &c:candidates_)if(c.device==record.device)c.requested=false;
  return true;
}
std::string ThreadGateway::ownership_digest_() const {
  std::vector<std::string> lines;
  for(const auto &r:ownership_)if(r.network==network_&&r.state!="pending")lines.push_back(r.device+":"+r.owner+":"+r.token+":"+r.state+":"+std::to_string(r.generation));
  std::sort(lines.begin(),lines.end());std::string all;for(auto &line:lines)all+=line+'\n';return thread_transport::sha256(all);
}
void ThreadGateway::public_record_(const Ownership &r) {
  auto *j=cJSON_CreateObject();cJSON_AddNumberToObject(j,"v",2);cJSON_AddStringToObject(j,"type","ownership");
  for(const auto &p:std::initializer_list<std::pair<const char *,std::string>>{{"network",r.network},{"device",r.device},{"node",r.node},{"name",r.name},{"node_type",r.node_type},{"owner",r.owner},{"binding_id",r.token},{"state",r.state}})cJSON_AddStringToObject(j,p.first,p.second.c_str());
  cJSON_AddNumberToObject(j,"generation",r.generation);otIp6Address group{};otIp6AddressFromString(group_.c_str(),&group);
  send_(group,discovery_port_,encode(j),true);cJSON_Delete(j);
}
bool ThreadGateway::request_binding_(Candidate &c) {
  if(!ledger_ready_||(!c.expected.empty()&&c.expected!=gateway_id_))return false;
  if(owner_(c.node)){ledger_error_="Resolve existing ownership before admission";return false;}
  for(const auto &r:ownership_)if(r.network==network_&&r.device==c.device&&r.owner==gateway_id_&&r.state=="pending") {if(c.token!=r.token)c.context.clear();c.token=r.token;c.requested=true;return true;}
  auto token=thread_transport::nonce();if(token.empty())return false;
  Ownership r{network_,c.device,c.node,c.name,c.node_type,gateway_id_,token,"pending",c.generation};
  if (!merge_ownership_(r)) {
    return false;
  }
  if (c.token != token) {
    c.context.clear();
  }
  c.token = token;
  c.requested = true;
  return true;
}
void ThreadGateway::send_candidate_(Candidate &c) {
  if(address_.empty()||c.session.empty()||c.port==0)return;
  const auto *owned=owner_(c.node);const bool active=owned&&owned->owner==gateway_id_&&owned->device==c.device;
  if(!active&&!c.requested)return;
  if(active){if(c.token!=owned->token)c.context.clear();c.token=owned->token;}
  if(c.context.empty())c.context=thread_transport::nonce().substr(0,16);
  if(c.context.empty())return;
  auto *j=cJSON_CreateObject();cJSON_AddNumberToObject(j,"v",2);cJSON_AddStringToObject(j,"type",active?"offer":"bind_request");
  for(const auto &p:std::initializer_list<std::pair<const char *,std::string>>{{"gateway_id",gateway_id_},{"network",network_},{"device",c.device},{"node",c.node},{"session",c.session},{"binding_id",c.token},{"ctx",c.context},{"address",address_}})cJSON_AddStringToObject(j,p.first,p.second.c_str());
  cJSON_AddNumberToObject(j,"request_id",c.request);cJSON_AddNumberToObject(j,"port",data_port_);cJSON_AddNumberToObject(j,"ttl_s",address_ttl_);
  // Reserve a runtime context before sending; a node may transmit immediately after its ACK.
  auto *node=find_node_(c.node);
  if(!node&&nodes_.size()<MAX_NODES){nodes_.emplace_back();node=&nodes_.back();node->id=c.node;node->name=c.name;node->node_type=c.node_type;}
  if(!node){cJSON_Delete(j);return;}
  if(node->session!=c.session||node->context!=c.context){
    if(!node->session.empty()&&node->session!=c.session){node->retired_sessions.push_back(node->session);if(node->retired_sessions.size()>4)node->retired_sessions.erase(node->retired_sessions.begin());}
    for(auto &command:commands_)if(command.node==node->id&&(command.status=="queued"||command.status=="waiting")){command.status="unconfirmed";command.error="Transport context changed";}
    node->report_receipts.clear();node->event_window=EventWindow{};node->has_sequence=false;node->manifest=Manifest{};
    node->schema_ready=false;node->expected_hash.clear();for(auto &e:node->entities)e.has_source_sequence=false;
  }
  node->network=network_;node->session=c.session;node->context=c.context;
  send_(c.address,c.port,encode(j),true);cJSON_Delete(j);
}
void ThreadGateway::reject_context_(const Packet &p,const cJSON *request,const char *reason) {
  const auto node=text(request,"node");const Ownership *record=owner_(node);
  if(std::strcmp(reason,"binding_revoked")==0) {
    record=nullptr;for(const auto &r:ownership_)if(r.network==network_&&r.node==node&&r.owner==gateway_id_&&r.state=="revoked"&&(!record||r.generation>record->generation))record=&r;
  }
  if(!record)return;
  auto *j=cJSON_CreateObject();cJSON_AddNumberToObject(j,"v",2);cJSON_AddStringToObject(j,"type",reason);
  for(const auto &pair:std::initializer_list<std::pair<const char *,std::string>>{{"network",record->network},{"device",record->device},{"node",record->node},{"owner",record->owner},{"binding_id",record->token},{"session",text(request,"session")}})cJSON_AddStringToObject(j,pair.first,pair.second.c_str());
  send_(p.address,p.port,encode(j),false);cJSON_Delete(j);
}
bool ThreadGateway::public_packet_(const Packet &p,const cJSON *j) {
  using namespace thread_transport;
  if(!ledger_ready_||network_.empty()||text(j,"network")!=network_)return false;
  const auto type=text(j,"type");
  if(type=="sync_request") {if(!sync_scheduled_){sync_scheduled_=true;sync_cursor_=0;}return true;}
  if(type=="announcement") {
    if(text(j,"digest")!=ownership_digest_()&&uint32_t(millis()-sync_request_at_)>=5000)sync_request_at_=0;
    return true;
  }
  if(type=="ownership") {
    Ownership r{network_,text(j,"device"),text(j,"node"),text(j,"name"),text(j,"node_type"),text(j,"owner"),text(j,"binding_id"),text(j,"state"),0};
    if(!hex_id(r.device,16)||!identifier(r.node)||r.name.size()>96||
       (r.node_type!="sed"&&r.node_type!="reed")||!thread_transport::uint32(field(j,"generation"))||
       (r.state!="active"&&r.state!="revoked"&&r.state!="unbound")||
       (r.state!="unbound"&&(!identifier(r.owner)||!hex_id(r.token,32))))return false;
    r.generation=field(j,"generation")->valuedouble;
    return merge_ownership_(r);
  }
  if(type=="bind_ack"||type=="binding_rejected") {
    auto *c=candidate_(text(j,"node"));
    if(!c||text(j,"device")!=c->device||text(j,"session")!=c->session||text(j,"ctx")!=c->context||
       p.port!=c->port||std::memcmp(&p.address,&c->address,sizeof(p.address))||
       !thread_transport::uint32(field(j,"request_id"))||uint32_t(field(j,"request_id")->valuedouble)!=c->request)return false;
    if(type=="binding_rejected"){
      c->requested=false;
      const auto owner=text(j,"owner"),token=text(j,"binding_id");
      if(identifier(owner)&&hex_id(token,32)&&thread_transport::uint32(field(j,"generation"))) {
        Ownership r{network_,c->device,c->node,c->name,c->node_type,owner,token,"active",uint32_t(field(j,"generation")->valuedouble)};
        merge_ownership_(r);
      }return true;
    }
    if(text(j,"owner")!=gateway_id_||text(j,"binding_id")!=c->token||!thread_transport::uint32(field(j,"generation")))return false;
    Ownership r{network_,c->device,c->node,c->name,c->node_type,gateway_id_,c->token,"active",uint32_t(field(j,"generation")->valuedouble)};
    if (!merge_ownership_(r)) {
      return false;
    }
    c->owner = gateway_id_;
    c->generation = r.generation;
    c->requested = false;
    public_record_(r);
    return true;
  }
  if(type!="discover")return false;
  const auto node=text(j,"node"),dev=text(j,"device"),session=text(j,"session"),owner=text(j,"owner"),token=text(j,"binding_id"),expected=text(j,"expected_gateway");
  if(!identifier(node)||!hex_id(dev,16)||!identifier(session)||text(j,"name").size()>96||
     (text(j,"node_type")!="sed"&&text(j,"node_type")!="reed")||
     (!owner.empty()&&!identifier(owner))||(!expected.empty()&&!identifier(expected))||
     (!owner.empty()&&!hex_id(token,32))||!thread_transport::uint32(field(j,"generation"))||!thread_transport::uint32(field(j,"request_id")))return false;
  for(const auto &record:ownership_)if(record.network==network_&&record.node==node&&record.device!=dev){ledger_error_="Node ID belongs to another physical device";return false;}
  if(auto *known=find_node_(node);known&&std::find(known->retired_sessions.begin(),known->retired_sessions.end(),session)!=known->retired_sessions.end())return false;
  auto *c=candidate_(node);
  if(c&&c->device!=dev)return false; // Stable identity collision: never replace another physical node.
  if(!c){if(candidates_.size()>=MAX_CANDIDATES)return false;candidates_.emplace_back();c=&candidates_.back();c->node=node;c->device=dev;}
  if(c->session!=session){c->context.clear();c->session=session;}
  c->name=text(j,"name");c->node_type=text(j,"node_type");c->expected=expected;c->owner=owner;
  c->manual_required=cJSON_IsTrue(field(j,"manual_required"));c->generation=field(j,"generation")->valuedouble;
  c->request=field(j,"request_id")->valuedouble;c->address=p.address;c->port=p.port;c->seen_at=millis();
  if(!owner.empty()) {
    if (c->token != token) {
      c->context.clear();
    }
    c->token = token;
    c->requested = false;
    if(revoked_(token)) {
      const Ownership *rev=nullptr;for(const auto &r:ownership_)if(r.token==token&&r.state=="revoked")rev=&r;
      if(rev){auto *out=cJSON_CreateObject();cJSON_AddNumberToObject(out,"v",2);cJSON_AddStringToObject(out,"type","binding_revoked");
        cJSON_AddStringToObject(out,"network",network_.c_str());cJSON_AddStringToObject(out,"device",dev.c_str());cJSON_AddStringToObject(out,"owner",rev->owner.c_str());cJSON_AddStringToObject(out,"binding_id",token.c_str());
        send_(p.address,p.port,encode(out),true);cJSON_Delete(out);}return true;
    }
    Ownership r{network_,dev,node,c->name,c->node_type,owner,token,"active",c->generation};
    if(!merge_ownership_(r))return false;
    if(owner==gateway_id_){send_candidate_(*c);}return true;
  }
  Ownership r{network_,dev,node,c->name,c->node_type,"","","unbound",c->generation};
  if(!merge_ownership_(r))return false;
  if(!expected.empty()&&expected==gateway_id_&&!c->manual_required) {
    if(!request_binding_(*c))return false;
  }
  // Manual acceptance survives sleep and gateway restart as a pending ledger record.
  for(const auto &pending:ownership_)if(pending.network==network_&&pending.device==dev&&pending.owner==gateway_id_&&pending.state=="pending"){if(c->token!=pending.token)c->context.clear();c->token=pending.token;c->requested=true;break;}
  send_candidate_(*c);return true;
}
void ThreadGateway::service_public_() {
  if(!attached_||network_.empty()||!ledger_ready_)return;
  const uint32_t now=millis();
  // Leave App-owned API objects alive until the user applies/reboots, but do
  // not let old foreign/unbound node caches consume future registry capacity.
  const auto count=nodes_.size();
  nodes_.erase(std::remove_if(nodes_.begin(),nodes_.end(),[&](const Node &node){
    const auto *c=candidate_(node.id);return !authorized_(node)&&!(c&&c->requested);
  }),nodes_.end());
  if(nodes_.size()!=count){dirty_=true;save_registry_();}
  if(sync_request_at_==0) {
    auto *j=cJSON_CreateObject();cJSON_AddNumberToObject(j,"v",2);cJSON_AddStringToObject(j,"type","sync_request");cJSON_AddStringToObject(j,"network",network_.c_str());
    otIp6Address group{};otIp6AddressFromString(group_.c_str(),&group);send_(group,discovery_port_,encode(j),true);cJSON_Delete(j);sync_request_at_=now?now:1;
  }
  if(sync_scheduled_&&uint32_t(now-sync_send_at_)>=50) {
    while(sync_cursor_<ownership_.size()&&(ownership_[sync_cursor_].network!=network_||ownership_[sync_cursor_].state=="pending"))++sync_cursor_;
    if(sync_cursor_<ownership_.size()){public_record_(ownership_[sync_cursor_++]);sync_send_at_=now;}
    else sync_scheduled_=false;
  }
  candidates_.erase(std::remove_if(candidates_.begin(),candidates_.end(),[&](const Candidate &c){return uint32_t(now-c.seen_at)>1800000&&!c.requested;}),candidates_.end());
}
bool ThreadGateway::revoke_node_(const std::string &node) {
  const auto *current=owner_(node);if(!current||current->owner!=gateway_id_){ledger_error_="Node is not owned by this gateway";return false;}
  auto revoked=*current;revoked.state="revoked";
  if(!merge_ownership_(revoked))return false;
  if(auto *n=find_node_(node)){
    if(!n->address.empty()&&n->port){Packet p{};otIp6AddressFromString(n->address.c_str(),&p.address);p.port=n->port;
      auto *j=cJSON_CreateObject();cJSON_AddStringToObject(j,"node",node.c_str());cJSON_AddStringToObject(j,"session",n->session.c_str());reject_context_(p,j,"binding_revoked");cJSON_Delete(j);}
    n->context.clear();n->schema_ready=false;
  }
  for(auto &c:candidates_)if(c.node==node)c.requested=false;
  public_record_(revoked);return true;
}
cJSON *ThreadGateway::binding_web_(const std::string &path,const cJSON *request,int &status) {
  if(path=="/thread_gateway/api/discovered") {
    auto *out=cJSON_CreateObject();auto *list=cJSON_AddArrayToObject(out,"nodes");cJSON_AddStringToObject(out,"ledger_error",ledger_error_.c_str());
    cJSON_AddBoolToObject(out,"synchronizing",sync_request_at_==0||sync_scheduled_);cJSON_AddNumberToObject(out,"ledger_records",ownership_.size());
    for(const auto &c:candidates_) {
      if(!c.expected.empty()&&c.expected!=gateway_id_)continue;
      if(owner_(c.node)||(!c.owner.empty()&&!revoked_(c.token)))continue;
      auto *j=cJSON_CreateObject();cJSON_AddItemToArray(list,j);cJSON_AddStringToObject(j,"id",c.node.c_str());cJSON_AddStringToObject(j,"name",c.name.c_str());cJSON_AddStringToObject(j,"node_type",c.node_type.c_str());
      cJSON_AddBoolToObject(j,"pending",c.requested);cJSON_AddBoolToObject(j,"waiting_for_revoke",!c.owner.empty());cJSON_AddNumberToObject(j,"last_seen_ms",uint32_t(millis()-c.seen_at));
    }return out;
  }
  if(!request)return error(status,405,"POST required");
  if(!cJSON_IsTrue(field(request,"confirm")))return error(status,400,"Admission confirmation required");
  auto *c=candidate_(text(request,"node"));if(!c)return error(status,404,"Discovered node not found");
  if(!c->owner.empty()||owner_(c->node))return error(status,409,"Node must finish unbinding first");
  if(!request_binding_(*c))return error(status,409,ledger_error_.empty()?"Admission unavailable":ledger_error_);
  send_candidate_(*c);auto *out=cJSON_CreateObject();cJSON_AddBoolToObject(out,"pending",true);return out;
}
}  // namespace esphome::thread_gateway
#endif
