#include "peripheral_pm.h"
#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <cerrno>

namespace esphome::deep_sleep_nrf52 {
namespace {
constexpr uint32_t PM_TIMEOUT_MS = 100;
struct PmRequest {
  const device *dev;
  pm_device_action action;
  bool direct;
  volatile uint32_t *baudrate;
  uint32_t saved_baudrate;
};
K_MSGQ_DEFINE(pm_requests, sizeof(PmRequest), 1, alignof(PmRequest));
K_SEM_DEFINE(pm_done, 0, 1);
atomic_t pm_busy{};
atomic_t pm_timeout{};
int pm_result;

void pm_worker(void *, void *, void *) {
  PmRequest request{};
  while (true) {
    k_msgq_get(&pm_requests, &request, K_FOREVER);
    if (atomic_get(&pm_timeout)) {
      pm_result = -ETIMEDOUT;
      k_sem_give(&pm_done);
      continue;
    }
    // Do not disable interrupts around a driver call: the caller's deadline
    // and the hardware watchdog must remain able to run.
    int result = request.direct ? request.dev->pm_base->action_cb(request.dev, request.action)
                                : pm_device_action_run(request.dev, request.action);
    if ((result == 0 || result == -EALREADY) && request.action == PM_DEVICE_ACTION_RESUME &&
        request.baudrate != nullptr)
      *request.baudrate = request.saved_baudrate;
    pm_result = result;
    k_sem_give(&pm_done);
  }
}
// Lowest preemptible application priority: a busy-waiting driver cannot starve
// the main thread when its semaphore timeout expires.
K_THREAD_DEFINE(pm_worker_thread, 2048, pm_worker, nullptr, nullptr, nullptr,
                K_LOWEST_APPLICATION_THREAD_PRIO, 0, 0);
}  // namespace

bool peripheral_pm_faulted() { return atomic_get(&pm_timeout) != 0; }

int peripheral_pm_call(const device *dev, pm_device_action action, bool direct,
                       volatile uint32_t *baudrate, uint32_t saved_baudrate) {
  if (peripheral_pm_faulted()) return -ETIMEDOUT;
  // The deadline must be able to preempt a driver spinning in this worker.
  if (k_thread_priority_get(k_current_get()) >= K_LOWEST_APPLICATION_THREAD_PRIO) return -ENOTSUP;
  if (!atomic_cas(&pm_busy, 0, 1)) return -EBUSY;
  k_sem_reset(&pm_done);
  const PmRequest request{dev, action, direct, baudrate, saved_baudrate};
  if (k_msgq_put(&pm_requests, &request, K_NO_WAIT) != 0) {
    atomic_clear(&pm_busy);
    return -EBUSY;
  }
  if (k_sem_take(&pm_done, K_MSEC(PM_TIMEOUT_MS)) != 0) {
    atomic_set(&pm_timeout, 1);
    return -ETIMEDOUT;
  }
  const int result = pm_result;
  atomic_clear(&pm_busy);
  return result;
}
}  // namespace esphome::deep_sleep_nrf52
#endif
