#pragma once
// Spatial-correlation math (M3): turning a declared correlation matrix R into
// the mixing matrix L that gives a lane vector its covariance.
//
// The generator draws one independent Jakes process per lane (M2, unchanged).
// Correlation is then a linear map across the lane axis:
//
//     g = L w,     E[g g^H] = L E[w w^H] L^H = L L^H = R
//
// so everything here exists to produce an L with L L^H = R.
//
// Lane order is l = r*Nt + t (rx outer, tx inner) -- the order resolve_topology
// already emits within a link -- and the convention this project uses is
//
//     E[g g^H] = R_rx (x) R_tx
//
// with the declared `tx` block used AS WRITTEN, neither transposed nor
// conjugated. Literature that column-stacks H (t outer) writes
// R_tx^T (x) R_rx; changing the lane order swaps the sides of the product but
// the transpose stays attached to R_tx, and it only bites on a COMPLEX R_tx.
// That makes it the quiet kind of wrong, so the convention is pinned here, the
// schema documents it, and the exit gate measures exactly this expression.

#include "ocudu_gpu_channel/config.h"
#include <complex>
#include <string>
#include <vector>

namespace ocg {

using CplxD = std::complex<double>;

// Largest lane count a correlated link may have (4x4). The device mixing kernel
// holds a lane vector in registers, so the bound is real and shared: the
// validator rejects above it rather than letting a topology reach a kernel that
// cannot represent it.
constexpr int kMaxCorrelatedLanes = 16;

// Dense row-major Hermitian matrix built from the sparse upper-triangle
// entries: unit diagonal, entry (i, j) as declared, (j, i) its conjugate.
// Assumes the entries have already been validated for range and duplicates.
std::vector<CplxD> dense_correlation(const std::vector<CorrelationEntry>& entries, int dim);

// Factorises a Hermitian PSD matrix into a mixing matrix M with M M^H = A.
//
// LDL^H, not Cholesky. Cholesky needs positive DEFINITE input and fails on a
// singular one, but a perfectly correlated pair (exactly 1.0, not 0.999...) is
// an input worth being able to test, and it is singular. LDL^H exists for every
// PSD matrix, yields M = L sqrt(D) directly, and -- the reason it is worth the
// few extra lines -- decides positive semidefiniteness and produces the factor
// in ONE routine, so validation and use cannot disagree about what is
// acceptable.
//
// Returns false and sets `error` when a pivot is negative beyond tolerance
// (i.e. the matrix is not PSD). Run once at prepare; never on the hot path.
bool correlation_mixing_matrix(const std::vector<CplxD>& hermitian, int dim,
                              std::vector<CplxD>& out_mixing, std::string& error);

// Kronecker product of two row-major square matrices.
// (A (x) B)[(ia*dim_b + ib), (ja*dim_b + jb)] = A[ia][ja] * B[ib][jb].
std::vector<CplxD> kron(const std::vector<CplxD>& a, int dim_a,
                        const std::vector<CplxD>& b, int dim_b);

// The lane mixing matrix for a link of dimensions (nt, nr), in lane order
// l = r*Nt + t. Identity when the model is iid, which is what keeps the M2
// path exactly as it was.
//
// For `kronecker` the two sides are factorised separately and their factors
// Kronecker-multiplied: (A (x) B) = (L_A (x) L_B)(L_A (x) L_B)^H, so this is
// exact and cheaper than factorising the Nt*Nr square.
bool lane_mixing_matrix(const SpatialCorrelationConfig& correlation, int nt, int nr,
                        std::vector<CplxD>& out_mixing, std::string& error);

// The per-lane LOS coefficients in lane order l = r*Nt + t. Lanes with no
// declared entry get 1 + 0j, so an undeclared matrix is the all-ones rank-1
// LOS -- one specular path seen by every antenna pair with the same phase.
// That is the minimum form of "the LOS phase is not drawn per lane"; it is not
// a claim about any particular array geometry.
std::vector<CplxD> lane_los_coefficients(const LosMatrixConfig& los, int nt, int nr);

} // namespace ocg
