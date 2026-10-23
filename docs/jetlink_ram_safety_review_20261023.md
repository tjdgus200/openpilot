# Shared-input memory review — 2026-10-23

## Scope and conclusion

This is a focused engineering review of today's DHCP cleanup and comma-local
shared-input IPC changes. It is **not AUTOSAR compliance, ISO 26262 compliance,
ASIL qualification, or proof of safe road use**. The implementation is Python,
NumPy, Linux IPC and an embedded shell/Python helper, not C++14. No qualified
static analysis, whole-system memory budget/WCET analysis, or safety case was
performed. Existing native GPU/USB libraries and OS behavior remain dependencies.

Reference: [AUTOSAR Guidelines for the use of the C++14 language in critical
and safety-related systems, section 6.18.5](https://www.autosar.org/fileadmin/standards/R22-11/AP/AUTOSAR_RS_CPP14Guidelines.pdf).
Its memory lifetime, allocation failure, fragmentation and timing concerns are
useful review criteria; C++ rules cannot be declared satisfied by these Python
tests. The project must first choose its applicable language/process standards.

## Implemented controls and evidence

| Hazard | Implemented control | Evidence / limitation |
|---|---|---|
| Oversized allocation | Negotiated contract and one-MiB per-slot upper bound | Zero, negative, oversized and arbitrarily large Python integers rejected; this is not a whole-process RAM cap |
| Buffer overrun / alias | Exact image shape/type/contiguity; fixed NumPy views; size seals; daemon read-only mapping | Same-submission pixel parity and sender/receiver payload equality; native copyout correctness still depends on tinygrad/driver |
| Timeout followed by overwrite | One in-flight slot; failure closes connection; next connection owns a new allocation | 100 retained-view reconnects; six device software USB losses and one delayed reply recovered in prior trial |
| Stale / foreign input | Session nonce, increasing generation, frame/reset checks and 150-ms token age limit | Duplicate, foreign and stale tokens rejected; this does not validate the semantic correctness of an image |
| Integer wrap | Python integers do not silently wrap; bounded wire packing rejects out-of-range values | Generation exhaustion and frame/source timestamp overflow close the client without sending; normal 32-bit frame wrap tested separately |
| Allocation exception | Connection/descriptor cleanup and existing local-model error path | Injected ENOMEM at memfd creation, truncate, reader mmap and writer mmap leaves no descriptor leak |
| Resource leakage | Close descriptors after mapping; retired views retain only their own mapping | 100 repeated connections return to the starting descriptor/mapping counts; not an indefinite soak or global lifetime proof |
| Unsafe DHCP reclamation | Verified ownership, SIGTERM, bounded five-second exit wait | Wrong owner never signaled; still-live helper retains ownership and blocks rebind |

For installed N16 one slot is 655,596 bytes (about 640 KiB). Two process
mappings refer to the same physical backing, rather than allocating two
independent image copies. The old socket path already had input buffers.
This does not mean total RSS drops by exactly 640 KiB: socket buffers,
temporary arrays, page accounting and other processes must also be measured.

## Important remaining limits

- A successful ftruncate/mmap is not proof that all future page faults can be
  serviced. This implementation does not reserve a whole-system memory budget,
  pre-fault/lock every page, or constrain allocation latency. Under severe
  memory pressure the OS can kill a process or fail a mapped-page access;
  those failures are not equivalent to a catchable Python MemoryError.
- The daemon creates a slot during handshake; Python and NumPy still allocate
  elsewhere during operation. There is no deterministic allocation/WCET proof.
- The one-MiB bound is per slot. Retained old views intentionally extend a
  mapping's lifetime. The tested owner is serial and releases its consumers,
  but there is no independent aggregate memory quota for all leaked views.
- A corrupted pixel that remains uint8, or a changed finite context value,
  can pass shape/finiteness checks. Nonces, size seals and generation checks
  do not provide ECC or end-to-end payload integrity. Test-time SHA digests
  verify these experiments; they are not a production corruption detector.
- Arbitrary concurrent writers are unsupported. Producer ownership is a
  program contract, not an OS-enforced immutable snapshot during inference.
- Existing fallback and commIssue handling are preserved, but takeover delay,
  process death, power loss and whole-vehicle hazard response have not been
  validated by the detached camera test. No road-safety conclusion follows.

## Next validation priorities

1. Establish total memory budget, low-memory margin and bounded reclamation
   under repeated disconnects, across all deployed models. Fault-inject page
   backing and allocation failures in an isolated worker/cgroup, not by
   exhausting the vehicle computer during operation.
2. Evaluate backing reservation/prefaulting at connection creation and its
   bounded failure response. Measure effects before adopting it; mlock alone
   is not an OOM safety strategy.
3. If the hazard analysis requires corruption detection, define an end-to-end
   integrity/snapshot contract with counter and checksum coverage, receiver
   verification and a defined fault response; quantify added work and P99.
4. Perform separate process-death and loss-of-inference response validation,
   longer memory/connection soak testing and independent safety review.

The default deployment was explicitly requested after the prior parity and
reconnect trials. It is a measured performance change with these limitations,
not a certified safety mechanism. Do not weaken cleanup, freshness, model
validation, USB power protection or update rollback to improve benchmarks.
