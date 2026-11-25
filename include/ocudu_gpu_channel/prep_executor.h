#pragma once

#include <cstddef>
#include <functional>
#include <memory>

namespace ocg {

// Persistent, caller-participating CPU worker pool shared by all receiving
// nodes of a CUDA processor for host-side preparation (input packing, power
// scans, host-fallback staging, and large output copies).
//
// Properties:
//   - Total participating threads per call is up to `threads` (calling thread
//     plus `threads - 1` background workers), avoiding an idle caller thread.
//   - Zero heap allocations on the hot `parallel_for()` path (job state lives
//     on the caller's stack until all participating workers leave the job).
//   - Safe under concurrent callers (e.g. per-RX-node broker producer threads):
//     each caller drains its own job directly and never deadlocks on a shared
//     barrier.
class PrepExecutor {
public:
  explicit PrepExecutor(std::size_t threads);
  ~PrepExecutor();
  PrepExecutor(const PrepExecutor&) = delete;
  PrepExecutor& operator=(const PrepExecutor&) = delete;

  std::size_t thread_count() const;

  // Executes `fn(index)` for every index in `[0, count)`, joining all workers
  // before returning or rethrowing the first exception thrown by any worker.
  void parallel_for(std::size_t count, const std::function<void(std::size_t)>& fn);

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

} // namespace ocg
