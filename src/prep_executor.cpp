#include "ocudu_gpu_channel/prep_executor.h"

#include <atomic>
#include <cstdint>
#include <exception>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

namespace ocg {

namespace {

inline void cpu_pause()
{
#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)
  __builtin_ia32_pause();
#elif defined(__aarch64__)
  asm volatile("yield" ::: "memory");
#else
  std::this_thread::yield();
#endif
}

constexpr std::size_t kMaxJobSlots = 64;
constexpr int kWorkerSpinIters = 1000;
constexpr int kCallerJoinSpinIters = 4000;

} // namespace

struct PrepExecutor::Impl {
  struct Job {
    const std::function<void(std::size_t)>* fn = nullptr;
    std::size_t total = 0;
    std::atomic<std::size_t> next{0};
    std::mutex err_mutex;
    std::exception_ptr error;
  };

  struct alignas(64) JobSlot {
    std::atomic<Job*> job{nullptr};
    // refcount == 0: slot is idle or draining has finished.
    // refcount > 0: caller holds 1 ref + each participating worker holds 1 ref.
    // Workers only acquire a ref via CAS from (cur > 0) -> (cur + 1), so once
    // refcount reaches 0, no worker can touch `job` again.
    std::atomic<std::uint32_t> refcount{0};
  };

  std::size_t threads;
  JobSlot slots[kMaxJobSlots];
  // Bit s is 1 while slot s is allocated to a caller (guards slot reuse).
  std::atomic<std::uint64_t> allocated_mask{0};
  // Bit s is 1 while slot s still advertises unclaimed work for workers.
  std::atomic<std::uint64_t> active_mask{0};
  // Monotonic wakeup counter for lock-free C++20 atomic::wait / notify_all.
  alignas(64) std::atomic<std::uint64_t> epoch{0};
  alignas(64) std::atomic<std::uint32_t> sleeping_workers{0};
  std::atomic<bool> stopping{false};
  std::vector<std::thread> workers;

  explicit Impl(std::size_t n) : threads(n)
  {
    if (n == 0) {
      throw std::invalid_argument("CUDA prep worker count must be positive");
    }
    if (n == 1) {
      return;
    }
    try {
      const std::size_t bg = n - 1;
      workers.reserve(bg);
      for (std::size_t i = 0; i < bg; ++i) {
        workers.emplace_back([this] { worker_loop(); });
      }
    } catch (...) {
      stop();
      throw;
    }
  }

  ~Impl() { stop(); }

  void stop()
  {
    stopping.store(true, std::memory_order_release);
    epoch.fetch_add(1, std::memory_order_acq_rel);
    epoch.notify_all();
    for (auto& w : workers) {
      if (w.joinable()) {
        w.join();
      }
    }
    workers.clear();
  }

  static void record_error(Job& job, std::exception_ptr err)
  {
    std::lock_guard<std::mutex> lock(job.err_mutex);
    if (!job.error) {
      job.error = std::move(err);
    }
  }

  // Acquire a free slot index in [0, kMaxJobSlots), or return kMaxJobSlots if full.
  std::size_t acquire_slot()
  {
    std::uint64_t cur = allocated_mask.load(std::memory_order_relaxed);
    while (cur != ~std::uint64_t{0}) {
      const std::size_t bit = static_cast<std::size_t>(__builtin_ctzll(~cur));
      const std::uint64_t next = cur | (std::uint64_t{1} << bit);
      if (allocated_mask.compare_exchange_weak(
              cur, next, std::memory_order_acquire, std::memory_order_relaxed)) {
        return bit;
      }
    }
    return kMaxJobSlots;
  }

  void release_slot(std::size_t s)
  {
    allocated_mask.fetch_and(~(std::uint64_t{1} << s), std::memory_order_release);
  }

  bool try_help_any_job()
  {
    std::uint64_t mask = active_mask.load(std::memory_order_acquire);
    while (mask != 0) {
      const std::size_t s = static_cast<std::size_t>(__builtin_ctzll(mask));
      mask &= (mask - 1);

      JobSlot& slot = slots[s];
      std::uint32_t rc = slot.refcount.load(std::memory_order_acquire);
      while (rc > 0) {
        if (slot.refcount.compare_exchange_weak(
                rc, rc + 1, std::memory_order_acq_rel, std::memory_order_acquire)) {
          Job* job = slot.job.load(std::memory_order_acquire);
          bool did_work = false;
          if (job != nullptr) {
            for (;;) {
              const std::size_t idx = job->next.fetch_add(1, std::memory_order_relaxed);
              if (idx >= job->total) {
                active_mask.fetch_and(~(std::uint64_t{1} << s), std::memory_order_release);
                break;
              }
              did_work = true;
              try {
                (*job->fn)(idx);
              } catch (...) {
                record_error(*job, std::current_exception());
              }
            }
          }
          if (slot.refcount.fetch_sub(1, std::memory_order_acq_rel) == 1) {
            slot.refcount.notify_one();
          }
          if (did_work) {
            return true;
          }
          break;
        }
      }
    }
    return false;
  }

  void worker_loop()
  {
    bool warm = false;
    for (;;) {
      if (try_help_any_job()) {
        warm = true;
        continue;
      }
      if (stopping.load(std::memory_order_acquire)) {
        return;
      }
      if (warm) {
        bool found = false;
        for (int spin = 0; spin < kWorkerSpinIters; ++spin) {
          if (active_mask.load(std::memory_order_acquire) != 0 ||
              stopping.load(std::memory_order_acquire)) {
            found = true;
            break;
          }
          cpu_pause();
        }
        if (found) {
          continue;
        }
        warm = false;
      }

      const std::uint64_t seen = epoch.load(std::memory_order_acquire);
      if (active_mask.load(std::memory_order_acquire) != 0 ||
          stopping.load(std::memory_order_acquire)) {
        continue;
      }
      sleeping_workers.fetch_add(1, std::memory_order_acq_rel);
      // Re-check after advertising sleep so a concurrent caller cannot miss us.
      if (active_mask.load(std::memory_order_acquire) == 0 &&
          !stopping.load(std::memory_order_acquire)) {
        epoch.wait(seen, std::memory_order_acquire);
      }
      sleeping_workers.fetch_sub(1, std::memory_order_acq_rel);
    }
  }
};

PrepExecutor::PrepExecutor(std::size_t threads) : impl_(std::make_unique<Impl>(threads)) {}
PrepExecutor::~PrepExecutor() = default;

std::size_t PrepExecutor::thread_count() const
{
  return impl_->threads;
}

void PrepExecutor::parallel_for(std::size_t count, const std::function<void(std::size_t)>& fn)
{
  if (count == 0) {
    return;
  }
  if (impl_->threads == 1 || count == 1) {
    for (std::size_t i = 0; i < count; ++i) {
      fn(i);
    }
    return;
  }

  const std::size_t slot_idx = impl_->acquire_slot();
  if (slot_idx == kMaxJobSlots) {
    // Saturated with 64 concurrent callers: execute directly on calling thread.
    for (std::size_t i = 0; i < count; ++i) {
      fn(i);
    }
    return;
  }

  Impl::Job job;
  job.fn = &fn;
  job.total = count;

  Impl::JobSlot& slot = impl_->slots[slot_idx];
  slot.job.store(&job, std::memory_order_release);
  // Publish refcount = 1 (caller's ref) BEFORE exposing slot in active_mask.
  slot.refcount.store(1, std::memory_order_release);
  impl_->active_mask.fetch_or(std::uint64_t{1} << slot_idx, std::memory_order_release);

  // Wake all sleeping workers in parallel via lock-free futex wake.
  impl_->epoch.fetch_add(1, std::memory_order_acq_rel);
  if (impl_->sleeping_workers.load(std::memory_order_acquire) != 0) {
    if (count == 2) {
      impl_->epoch.notify_one();
    } else {
      impl_->epoch.notify_all();
    }
  }

  // Caller participates directly so it never sits idle waiting for workers.
  for (;;) {
    const std::size_t idx = job.next.fetch_add(1, std::memory_order_relaxed);
    if (idx >= count) {
      break;
    }
    try {
      fn(idx);
    } catch (...) {
      Impl::record_error(job, std::current_exception());
    }
  }

  // Hide slot from new workers, then drop caller's ref and wait for any
  // in-flight workers to finish before unwinding stack-allocated `job`.
  impl_->active_mask.fetch_and(~(std::uint64_t{1} << slot_idx), std::memory_order_release);
  const std::uint32_t prev = slot.refcount.fetch_sub(1, std::memory_order_acq_rel);
  if (prev > 1) {
    for (int spin = 0; spin < kCallerJoinSpinIters; ++spin) {
      if (slot.refcount.load(std::memory_order_acquire) == 0) {
        break;
      }
      cpu_pause();
    }
    std::uint32_t cur = slot.refcount.load(std::memory_order_acquire);
    while (cur != 0) {
      slot.refcount.wait(cur, std::memory_order_acquire);
      cur = slot.refcount.load(std::memory_order_acquire);
    }
  }

  slot.job.store(nullptr, std::memory_order_release);
  impl_->release_slot(slot_idx);

  if (job.error) {
    std::rethrow_exception(job.error);
  }
}

} // namespace ocg
