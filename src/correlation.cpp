#include "ocudu_gpu_channel/correlation.h"
#include <cmath>

namespace ocg {
namespace {

// A pivot this small is treated as an exactly-zero direction rather than a
// negative one: a PSD matrix of rank < dim (perfect correlation, for instance)
// has zero pivots, and rounding can push them a hair either way.
constexpr double kPivotEpsilon = 1e-12;

} // namespace

std::vector<CplxD> dense_correlation(const std::vector<CorrelationEntry>& entries, int dim)
{
  std::vector<CplxD> out(static_cast<std::size_t>(dim) * static_cast<std::size_t>(dim),
                         CplxD{0.0, 0.0});
  for (int d = 0; d != dim; ++d) {
    out[static_cast<std::size_t>(d) * dim + d] = CplxD{1.0, 0.0};
  }
  for (const auto& e : entries) {
    if (e.i < 0 || e.j < 0 || e.i >= dim || e.j >= dim) {
      continue; // out-of-range entries are a validator error, not this function's
    }
    const CplxD value{e.re, e.im};
    out[static_cast<std::size_t>(e.i) * dim + e.j] = value;
    out[static_cast<std::size_t>(e.j) * dim + e.i] = std::conj(value);
  }
  return out;
}

bool correlation_mixing_matrix(const std::vector<CplxD>& hermitian, int dim,
                              std::vector<CplxD>& out_mixing, std::string& error)
{
  const auto n = static_cast<std::size_t>(dim);
  if (dim <= 0 || hermitian.size() != n * n) {
    error = "correlation matrix has the wrong size";
    return false;
  }
  // L is unit lower triangular, d is the real diagonal of D.
  std::vector<CplxD> lower(n * n, CplxD{0.0, 0.0});
  std::vector<double> d(n, 0.0);

  for (std::size_t j = 0; j != n; ++j) {
    CplxD sum{0.0, 0.0};
    for (std::size_t k = 0; k != j; ++k) {
      sum += lower[j * n + k] * std::conj(lower[j * n + k]) * d[k];
    }
    d[j] = hermitian[j * n + j].real() - sum.real();
    if (d[j] < -kPivotEpsilon) {
      error = "correlation matrix is not positive semidefinite (negative pivot " +
              std::to_string(d[j]) + " at index " + std::to_string(j) + ")";
      return false;
    }
    lower[j * n + j] = CplxD{1.0, 0.0};
    for (std::size_t i = j + 1; i != n; ++i) {
      CplxD acc{0.0, 0.0};
      for (std::size_t k = 0; k != j; ++k) {
        acc += lower[i * n + k] * std::conj(lower[j * n + k]) * d[k];
      }
      if (d[j] <= kPivotEpsilon) {
        // A zero pivot means this direction carries no variance. Every entry
        // below it must then be zero too; if the residual is not, the matrix
        // was not PSD after all (a zero variance cannot have covariance).
        if (std::abs(hermitian[i * n + j] - acc) > 1e-9) {
          error = "correlation matrix is not positive semidefinite (zero pivot at index " +
                  std::to_string(j) + " with a nonzero column below it)";
          return false;
        }
        lower[i * n + j] = CplxD{0.0, 0.0};
      } else {
        lower[i * n + j] = (hermitian[i * n + j] - acc) / d[j];
      }
    }
  }

  // M = L sqrt(D): scale column j by sqrt(d[j]). Then M M^H = L D L^H = A.
  out_mixing.assign(n * n, CplxD{0.0, 0.0});
  for (std::size_t j = 0; j != n; ++j) {
    const double scale = std::sqrt(std::max(0.0, d[j]));
    for (std::size_t i = j; i != n; ++i) {
      out_mixing[i * n + j] = lower[i * n + j] * scale;
    }
  }
  error.clear();
  return true;
}

std::vector<CplxD> kron(const std::vector<CplxD>& a, int dim_a,
                        const std::vector<CplxD>& b, int dim_b)
{
  const auto na = static_cast<std::size_t>(dim_a);
  const auto nb = static_cast<std::size_t>(dim_b);
  const std::size_t n = na * nb;
  std::vector<CplxD> out(n * n, CplxD{0.0, 0.0});
  for (std::size_t ia = 0; ia != na; ++ia) {
    for (std::size_t ja = 0; ja != na; ++ja) {
      const CplxD av = a[ia * na + ja];
      if (av == CplxD{0.0, 0.0}) {
        continue;
      }
      for (std::size_t ib = 0; ib != nb; ++ib) {
        for (std::size_t jb = 0; jb != nb; ++jb) {
          out[(ia * nb + ib) * n + (ja * nb + jb)] = av * b[ib * nb + jb];
        }
      }
    }
  }
  return out;
}

bool lane_mixing_matrix(const SpatialCorrelationConfig& correlation, int nt, int nr,
                        std::vector<CplxD>& out_mixing, std::string& error)
{
  const auto lanes = static_cast<std::size_t>(nt) * static_cast<std::size_t>(nr);
  if (nt <= 0 || nr <= 0) {
    error = "lane mixing needs positive Nt and Nr";
    return false;
  }
  if (correlation.kind == SpatialCorrelationKind::Iid) {
    out_mixing.assign(lanes * lanes, CplxD{0.0, 0.0});
    for (std::size_t l = 0; l != lanes; ++l) {
      out_mixing[l * lanes + l] = CplxD{1.0, 0.0};
    }
    error.clear();
    return true;
  }

  std::vector<CplxD> mix_rx;
  std::vector<CplxD> mix_tx;
  if (!correlation_mixing_matrix(dense_correlation(correlation.rx, nr), nr, mix_rx, error)) {
    error = "rx correlation: " + error;
    return false;
  }
  if (!correlation_mixing_matrix(dense_correlation(correlation.tx, nt), nt, mix_tx, error)) {
    error = "tx correlation: " + error;
    return false;
  }
  // Lane order l = r*Nt + t puts rx on the OUTER index, so the rx factor is the
  // left operand of the Kronecker product.
  out_mixing = kron(mix_rx, nr, mix_tx, nt);
  error.clear();
  return true;
}

std::vector<CplxD> lane_los_coefficients(const LosMatrixConfig& los, int nt, int nr)
{
  const auto lanes = static_cast<std::size_t>(nt) * static_cast<std::size_t>(nr);
  std::vector<CplxD> out(lanes, CplxD{1.0, 0.0});
  if (!los.declared) {
    return out;
  }
  for (const auto& c : los.coefficients) {
    if (c.rx < 0 || c.tx < 0 || c.rx >= nr || c.tx >= nt) {
      continue; // range is a validator error
    }
    out[static_cast<std::size_t>(c.rx) * nt + c.tx] = CplxD{c.re, c.im};
  }
  return out;
}

} // namespace ocg
