#include <NvInfer.h>
#include <cuda_fp16.h>
#include <cuda_runtime.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <exception>
#include <initializer_list>
#include <memory>
#include <stdexcept>
#include "kernels.cuh"

namespace {
using namespace pv5_yolo;
class Logger final : public nvinfer1::ILogger {
  void log(Severity severity, const char *message) noexcept override {
    if (severity <= Severity::kERROR) std::fprintf(stderr, "YOLO TensorRT: %.400s\n", message);
  }
};
void check(cudaError_t result) {
  if (result != cudaSuccess) throw std::runtime_error(cudaGetErrorString(result));
}
void error_text(char *out, size_t size, const char *message) noexcept {
  if (out && size) std::snprintf(out, size, "%s", message);
}

struct Detector {
  Logger logger;
  std::unique_ptr<nvinfer1::IRuntime> runtime;
  std::unique_ptr<nvinfer1::ICudaEngine> engine;
  std::unique_ptr<nvinfer1::IExecutionContext> context;
  cudaStream_t stream = nullptr;
  __half *input = nullptr, *output = nullptr;
  unsigned char *source = nullptr, *device_source = nullptr;
  ResultBuffer *results = nullptr, *device_results = nullptr;
  size_t source_capacity = 0;
  bool poisoned = false;

  ~Detector() {
    if (stream) cudaStreamSynchronize(stream);
    context.reset();
    engine.reset();
    runtime.reset();
    if (input) cudaFree(input);
    if (output) cudaFree(output);
    if (source) cudaFreeHost(source);
    if (results) cudaFreeHost(results);
    if (stream) cudaStreamDestroy(stream);
  }

  bool tensor(const char *name, nvinfer1::TensorIOMode mode, std::initializer_list<int> dims) const {
    if (engine->getTensorIOMode(name) != mode ||
        engine->getTensorDataType(name) != nvinfer1::DataType::kHALF ||
        engine->getTensorLocation(name) != nvinfer1::TensorLocation::kDEVICE ||
        engine->getTensorFormat(name) != nvinfer1::TensorFormat::kLINEAR) return false;
    const auto shape = engine->getTensorShape(name);
    return shape.nbDims == int(dims.size()) && std::equal(dims.begin(), dims.end(), shape.d);
  }

  void init(const void *plan, size_t length, size_t capacity) {
    source_capacity = capacity;
    runtime.reset(nvinfer1::createInferRuntime(logger));
    if (!runtime) throw std::runtime_error("TensorRT runtime creation failed");
    engine.reset(runtime->deserializeCudaEngine(plan, length));
    if (!engine || engine->getNbIOTensors() != 2 || engine->getNbAuxStreams() != 0 ||
        !tensor("images", nvinfer1::TensorIOMode::kINPUT, {1,3,kSide,kSide}) ||
        !tensor("output0", nvinfer1::TensorIOMode::kOUTPUT, {1,kRows,kColumns}))
      throw std::runtime_error("expected single-stream YOLOv5n v7 FP16 plan, static linear images/output0");
    int least, greatest;
    check(cudaDeviceGetStreamPriorityRange(&least, &greatest));
    check(cudaStreamCreateWithPriority(&stream, cudaStreamNonBlocking, least));
    context.reset(engine->createExecutionContext());
    if (!context) throw std::runtime_error("TensorRT execution context creation failed");
    check(cudaMalloc(&input, 3 * kSide * kSide * sizeof(__half)));
    check(cudaMalloc(&output, kRows * kColumns * sizeof(__half)));
    // Jetson's shared memory: kernels read the sampled frame and write the compact result directly.
    check(cudaHostAlloc(&source, capacity, cudaHostAllocMapped));
    check(cudaHostGetDevicePointer(&device_source, source, 0));
    check(cudaHostAlloc(&results, sizeof(ResultBuffer), cudaHostAllocMapped));
    check(cudaHostGetDevicePointer(&device_results, results, 0));
    if (!context->setTensorAddress("images", input) || !context->setTensorAddress("output0", output))
      throw std::runtime_error("failed to bind YOLO tensors");
  }
};
} // namespace

extern "C" void *pv5_yolo_open(const void *plan, size_t length, size_t capacity, char *error, size_t size) noexcept {
  try {
    auto detector = std::make_unique<Detector>();
    detector->init(plan, length, capacity);
    return detector.release();
  } catch (const std::exception &e) { error_text(error, size, e.what()); return nullptr; }
}

extern "C" void pv5_yolo_close(void *state) noexcept { delete static_cast<Detector *>(state); }

extern "C" int pv5_yolo_detect(void *state, const unsigned char *src, size_t length, int w, int h,
                               int stride, int six, int rw, int rh, Candidate *out,
                               char *error, size_t size) noexcept {
  auto &d = *static_cast<Detector *>(state);
  try {
    if (d.poisoned) throw std::runtime_error("YOLO execution context requires reopening");
    if (length > d.source_capacity) throw std::runtime_error("frame exceeds detector input capacity");
    std::memcpy(d.source, src, length);
    d.results->count = 0;
    d.poisoned = true;  // In-flight failures cannot safely reuse this context or its mapped buffers.
    preprocess<<<(kSide*kSide + 255)/256, 256, 0, d.stream>>>(d.device_source, w, h, stride, six, rw, rh, d.input);
    check(cudaGetLastError());
    if (!d.context->enqueueV3(d.stream)) throw std::runtime_error("TensorRT enqueueV3 failed");
    decode<<<(kRows + 255)/256, 256, 0, d.stream>>>(d.output, d.device_results);
    check(cudaGetLastError());
    check(cudaStreamSynchronize(d.stream));  // Worker-owned stream only.
    d.poisoned = false;
    const int count = d.results->count;
    if (count < 0) throw std::runtime_error("invalid YOLO candidate count");
    // A crowded frame cannot safely use a race-dependent truncated candidate set.
    // Abstain on this frame; the next valid sample can still be processed.
    if (count > kCapacity) return 0;
    std::memcpy(out, d.results->rows, count * sizeof(Candidate));
    return count;
  } catch (const std::exception &e) { error_text(error, size, e.what()); return -1; }
}
