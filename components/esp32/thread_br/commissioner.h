#pragma once
#include "sdkconfig.h"
#ifdef CONFIG_THREAD_BR_COMMISSIONER_WEB
#include <array>
#include <atomic>
#include <cstdint>
#include <string>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "openthread/commissioner.h"
#include "openthread/thread.h"
#include "cJSON.h"
#include "commissioner_discovery.h"

namespace esphome::thread_br {
class CommissionerManager {
 public:
  void configure(const std::string &id, const std::string &network) { id_ = id; network_json_ = network; }
  void setup();
  void loop(bool ready);
  void on_shutdown();
  void dump_config();

 protected:
  struct Candidate {
    char id[19]{}, address[17]{}, device_id[17]{}, name[49]{};
    uint32_t seen{0}; uint8_t version{0}; bool used{false};
  };
  struct Event {
    uint32_t at; otExtAddress address; uint8_t kind, value;
    otExtAddress device_id{}; char name[49]{}; bool identified{false};
  };
  struct Request { char pskd[33]{}, candidate[19]{}; otExtAddress eui{}; bool has_eui{false}; uint16_t seconds{120}; };
  static void discovery_(const otThreadDiscoveryRequestInfo *, void *);
  static void identified_discovery_(const otExtAddress *, uint8_t, const otExtAddress *, const char *, void *);
  static void state_(otCommissionerState, void *);
  static void joiner_(otCommissionerJoinerEvent, const otJoinerInfo *, const otExtAddress *, void *);
  static cJSON *get_(void *context, int *status);
  static cJSON *post_(void *context, const cJSON *request, int *status);
  cJSON *snapshot_(int &status);
  cJSON *handle_request_(const cJSON *request, int &status);
  bool network_matches_(otInstance *instance) const;
  void close_(otInstance *, const char *phase, const char *message);
  void event_(const Event &, otInstance *);
  bool running_() const { return queued_ || owns_; }
  std::string id_, network_json_;
  std::string active_omr_prefix_;
  cJSON *network_{nullptr};
  SemaphoreHandle_t mutex_{nullptr};
  QueueHandle_t events_{nullptr};
  std::array<Candidate, 32> candidates_{};
  Request request_;
  const char *phase_{"idle"}, *message_{"Waiting for Thread attachment"};
  uint32_t requested_at_{0}, window_at_{0}, dataset_check_at_{0};
  bool ready_{false}, matched_{false}, registered_{false}, queued_{false}, cancel_{false};
  bool owns_{false}, authorized_{false}, finalized_{false};
  std::atomic<bool> shutdown_{false};
  std::atomic<uint32_t> dropped_{0};
};
}  // namespace esphome::thread_br
#endif
