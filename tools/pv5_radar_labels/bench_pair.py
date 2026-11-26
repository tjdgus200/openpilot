#!/usr/bin/env python3
"""Offline 20 Hz TensorRT main-engine and native YOLO A/B benchmark.

Use compatible plans and a shared build of native/detector.cu on an idle
Linux CUDA bench. This tool opens no USB transports or services. The main
engine receives fixed inputs and copies all outputs back, with no vehicle
model state feedback. The auxiliary process runs the native YUV6 detector.
"""
import argparse
from bisect import bisect_right
import ctypes as C
import hashlib
import json
import math
import multiprocessing as MP
import platform
import socket
import struct
import time
from pathlib import Path


PERIOD_NS = 50_000_000
OFFER_NS = 4 * PERIOD_NS
WIDTH, HEIGHT = 512, 256
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2
JOB = struct.Struct("!QQ")
DONE = struct.Struct("!QQQII")
NOW = time.perf_counter_ns


class Cuda:
    def __init__(self, library):
        self.lib = C.CDLL(library)
        self.lib.cudaGetErrorString.argtypes = [C.c_int]
        self.lib.cudaGetErrorString.restype = C.c_char_p
        pointer = C.POINTER(C.c_void_p)
        size = C.c_size_t
        address = C.c_void_p
        signatures = {
            "cudaMalloc": [pointer, size],
            "cudaMallocHost": [pointer, size],
            "cudaFree": [address],
            "cudaFreeHost": [address],
            "cudaStreamCreateWithFlags": [pointer, C.c_uint],
            "cudaStreamSynchronize": [address],
            "cudaStreamDestroy": [address],
            "cudaMemcpyAsync": [address, address, size, C.c_int, address],
            "cudaEventCreate": [pointer],
            "cudaEventDestroy": [address],
            "cudaEventRecord": [address, address],
            "cudaEventRecordWithFlags": [address, address, C.c_uint],
            "cudaEventElapsedTime": [C.POINTER(C.c_float), address, address],
            "cudaStreamBeginCapture": [address, C.c_int],
            "cudaStreamEndCapture": [address, pointer],
            "cudaGraphInstantiateWithFlags": [pointer, address, C.c_ulonglong],
            "cudaGraphLaunch": [address, address],
            "cudaGraphDestroy": [address],
            "cudaGraphExecDestroy": [address],
        }
        for name, args in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            fn.restype = C.c_int

    def call(self, name, *args):
        status = getattr(self.lib, name)(*args)
        if status:
            why = self.lib.cudaGetErrorString(status).decode("utf-8", "replace")
            raise RuntimeError(f"{name} failed ({status}): {why}")

    def create(self, name, *args):
        result = C.c_void_p()
        self.call(name, C.byref(result), *args)
        return result


class MainEngine:
    def __init__(self, plan, cuda_library, frame):
        import tensorrt as trt

        self.cuda = Cuda(cuda_library)
        self.trt_version = trt.__version__
        self.logger = trt.Logger(trt.Logger.ERROR)
        self.runtime = trt.Runtime(self.logger)
        self.engine = self.runtime.deserialize_cuda_engine(plan.read_bytes())
        if self.engine is None:
            raise RuntimeError(f"TensorRT deserialization failed: {plan}")
        self.context = self.engine.create_execution_context()
        if self.context is None:
            raise RuntimeError("TensorRT execution context creation failed")
        self.stream = self.cuda.create("cudaStreamCreateWithFlags", 1)
        self.begin = self.cuda.create("cudaEventCreate")
        self.end = self.cuda.create("cudaEventCreate")
        self.compute_begin = self.cuda.create("cudaEventCreate")
        self.compute_end = self.cuda.create("cudaEventCreate")
        self.elapsed = C.c_float()
        self.bindings = []
        self.metadata = []
        self.graph = None
        self.graph_exec = None
        for ix in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(ix)
            dtype = self.engine.get_tensor_dtype(name)
            shape = tuple(self.context.get_tensor_shape(name))
            if min(shape, default=1) <= 0 or self.engine.get_tensor_format(name) != trt.TensorFormat.LINEAR:
                raise ValueError(f"{name} requires static linear TensorRT I/O, got {shape}")
            if self.engine.get_tensor_location(name) != trt.TensorLocation.DEVICE:
                raise ValueError(f"{name} requires unsupported host tensor binding")
            nbytes = math.prod(shape) * dtype.itemsize
            mem = self.cuda.create("cudaMalloc", nbytes)
            host = self.cuda.create("cudaMallocHost", nbytes)
            is_input = self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT
            if not self.context.set_tensor_address(name, mem.value):
                raise RuntimeError(f"failed to bind {name}")
            self.bindings.append((is_input, host, mem, nbytes))
            self.metadata.append({"name": name, "shape": shape, "dtype": str(dtype),
                                  "input": is_input, "bytes": nbytes})
            C.memset(host, 0, nbytes)
            if is_input and name == "new_img" and nbytes == len(frame) * 2:
                C.memmove(host, frame, len(frame))
                C.memmove(host.value + len(frame), frame, len(frame))

    def enqueue(self, graph_capture=False):
        for is_input, host, mem, nbytes in self.bindings:
            if is_input:
                self.cuda.call("cudaMemcpyAsync", mem, host, nbytes, 1, self.stream)
        # External event nodes retain timing across repeated graph launches.
        flags = int(graph_capture)
        self.cuda.call("cudaEventRecordWithFlags", self.compute_begin, self.stream, flags)
        if not self.context.execute_async_v3(self.stream.value):
            raise RuntimeError("TensorRT execute_async_v3 failed")
        self.cuda.call("cudaEventRecordWithFlags", self.compute_end, self.stream, flags)
        for is_input, host, mem, nbytes in self.bindings:
            if not is_input:
                self.cuda.call("cudaMemcpyAsync", host, mem, nbytes, 2, self.stream)

    def capture(self):
        self.cuda.call("cudaStreamBeginCapture", self.stream, 1)
        graph = C.c_void_p()
        try:
            self.enqueue(graph_capture=True)
        finally:
            self.cuda.call("cudaStreamEndCapture", self.stream, C.byref(graph))
            self.graph = graph
        self.graph_exec = self.cuda.create("cudaGraphInstantiateWithFlags", self.graph, 0)

    def run(self):
        self.cuda.call("cudaEventRecord", self.begin, self.stream)
        if self.graph_exec is None:
            self.enqueue()
        else:
            self.cuda.call("cudaGraphLaunch", self.graph_exec, self.stream)
        self.cuda.call("cudaEventRecord", self.end, self.stream)
        self.cuda.call("cudaStreamSynchronize", self.stream)
        reply_ns = NOW()
        self.cuda.call("cudaEventElapsedTime", C.byref(self.elapsed), self.begin, self.end)
        service = self.elapsed.value
        self.cuda.call("cudaEventElapsedTime", C.byref(self.elapsed), self.compute_begin, self.compute_end)
        return reply_ns, service, self.elapsed.value

    def close(self):
        self.cuda.call("cudaStreamSynchronize", self.stream)
        if self.graph_exec is not None:
            self.cuda.call("cudaGraphExecDestroy", self.graph_exec)
        if self.graph is not None:
            self.cuda.call("cudaGraphDestroy", self.graph)
        self.context = self.engine = self.runtime = None
        for _, host, mem, _ in self.bindings:
            self.cuda.call("cudaFreeHost", host)
            self.cuda.call("cudaFree", mem)
        self.cuda.call("cudaEventDestroy", self.end)
        self.cuda.call("cudaEventDestroy", self.begin)
        self.cuda.call("cudaEventDestroy", self.compute_end)
        self.cuda.call("cudaEventDestroy", self.compute_begin)
        self.cuda.call("cudaStreamDestroy", self.stream)

    def output_hashes(self):
        return {meta["name"]: hashlib.sha256(C.string_at(host, nbytes)).hexdigest()
                for meta, (is_input, host, _, nbytes) in zip(self.metadata, self.bindings, strict=True)
                if not is_input}


class Candidate(C.Structure):
    _fields_ = [("bbox", C.c_float * 4), ("confidence", C.c_float),
                ("class_id", C.c_int32), ("anchor", C.c_int32)]


def overlaps(a, b):
    x1, y1, x2, y2 = a
    xx1, yy1, xx2, yy2 = b
    inter = max(0, min(x2, xx2) - max(x1, xx1)) * max(0, min(y2, yy2) - max(y1, yy1))
    union = (x2 - x1) * (y2 - y1) + (xx2 - xx1) * (yy2 - yy1) - inter
    return inter > 0.45 * union


def retained_count(rows):
    by_class = {}
    selected = 0
    # Same class-aware NMS threshold/order and letterbox clipping as Detector.
    for row in sorted(rows, key=lambda c: (-c.confidence, c.anchor)):
        held = by_class.setdefault(row.class_id, [])
        if any(overlaps(old, row.bbox) for old in held):
            continue
        held.append(tuple(row.bbox))
        selected += 1
        if selected == 300:
            break
    return sum(min(x2, 640) > max(x1, 0) and min(y2, 480) > max(y1, 160)
               for boxes in by_class.values() for x1, y1, x2, y2 in boxes)


def yolo_worker(link, ready, frame, library, plan):
    state = None
    try:
        lib = C.CDLL(str(library))
        lib.pv5_yolo_open.argtypes = [C.c_void_p, C.c_size_t, C.c_size_t, C.c_char_p, C.c_size_t]
        lib.pv5_yolo_open.restype = C.c_void_p
        lib.pv5_yolo_close.argtypes = [C.c_void_p]
        lib.pv5_yolo_close.restype = None
        lib.pv5_yolo_detect.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, *([C.c_int] * 6),
                                      C.POINTER(Candidate), C.c_char_p, C.c_size_t]
        lib.pv5_yolo_detect.restype = C.c_int
        blob = plan.read_bytes()
        error = C.create_string_buffer(256)
        state = lib.pv5_yolo_open(blob, len(blob), FRAME_BYTES, error, len(error))
        if not state:
            raise RuntimeError(error.value.decode("utf-8", "replace"))
        del blob
        rows = (Candidate * 2048)()
        source = C.addressof(frame)
        ready.send("")
        ready.close()
        while data := link.recv(JOB.size):
            seq, earliest = JOB.unpack(data)
            if not seq:
                break
            remaining = earliest - NOW()
            if remaining > 0:
                time.sleep(remaining / 1e9)
            begin = NOW()
            error.value = b""
            count = lib.pv5_yolo_detect(state, source, FRAME_BYTES, WIDTH, HEIGHT,
                                        0, 1, 640, 320, rows, error, len(error))
            if count < 0:
                raise RuntimeError(error.value.decode("utf-8", "replace"))
            kept = retained_count(rows[:count])
            link.send(DONE.pack(seq, begin, NOW(), count, kept))
    except Exception as exc:
        if not ready.closed:
            ready.send(str(exc))
        raise
    finally:
        if state is not None:
            lib.pv5_yolo_close(state)
        ready.close()
        link.close()


class YoloProcess:
    def __init__(self, library, plan, frame):
        context = MP.get_context("spawn")
        self.shared = context.RawArray(C.c_uint8, FRAME_BYTES)
        self.link, remote = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        ready, signal = context.Pipe(duplex=False)
        self.process = context.Process(target=yolo_worker, args=(remote, signal, self.shared, library, plan))
        self.process.start()
        remote.close()
        signal.close()
        self.link.setblocking(False)
        self.frame = frame
        self.in_flight = None
        self.seq = 0
        try:
            if not ready.poll(90):
                raise TimeoutError("native YOLO worker startup timed out")
            if (error := ready.recv()):
                raise RuntimeError(error)
        except BaseException:
            self.close()
            raise
        finally:
            ready.close()

    def take(self):
        try:
            job, start, end, candidates, retained = DONE.unpack(self.link.recv(DONE.size))
        except BlockingIOError:
            if self.process.exitcode is not None:
                raise RuntimeError(f"native YOLO worker exited ({self.process.exitcode})")
            return None
        if self.in_flight is None or job != self.in_flight[0]:
            raise RuntimeError("unexpected native YOLO reply")
        _, offered, earliest = self.in_flight
        self.in_flight = None
        return {"offered_ns": offered, "earliest_ns": earliest, "start_ns": start, "end_ns": end,
                "candidates": candidates, "retained": retained}

    def offer(self, earliest=0):
        if self.in_flight is not None:
            return False
        self.seq += 1
        offered = NOW()
        C.memmove(C.addressof(self.shared), self.frame, FRAME_BYTES)
        try:
            self.link.send(JOB.pack(self.seq, earliest))
        except BlockingIOError:
            return False
        self.in_flight = (self.seq, offered, earliest)
        return True

    def drain(self):
        deadline = NOW() + 5_000_000_000
        while self.in_flight is not None:
            if (row := self.take()) is not None:
                return row
            if NOW() > deadline:
                raise TimeoutError("native YOLO job did not complete within five seconds")
            time.sleep(0.0005)
        return None

    def close(self):
        try:
            self.link.send(JOB.pack(0, 0))
        except OSError:
            pass
        self.process.join(2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(2)
        self.link.close()


def paced(engine, worker, slots, after, worker_start_ns=0):
    rows, jobs = [], []
    dropped, offers, admitted, busy = 0, 0, 0, 0
    start = NOW() + PERIOD_NS
    for seq in range(slots):
        due = start + seq * PERIOD_NS
        now = NOW()
        if now >= due + PERIOD_NS:
            dropped += 1
            continue
        if now < due:
            time.sleep((due - now) / 1e9)
        begin = NOW()
        if begin >= due + PERIOD_NS:
            dropped += 1
            continue
        reply, gpu_ms, compute_ms = engine.run()
        rows.append({"slot": seq, "scheduled_ns": due, "start_ns": begin, "reply_ns": reply,
                     "gpu_ms": gpu_ms, "trt_gpu_ms": compute_ms})
        if worker:
            if (past := worker.take()) is not None:
                jobs.append(past)
            # Keep this throttle across adjacent B blocks, after each main reply.
            if reply >= after:
                offers += 1
                if worker.offer(due + worker_start_ns):
                    admitted += 1
                else:
                    busy += 1
                after = NOW() + OFFER_NS
    if worker and (tail := worker.drain()) is not None:
        jobs.append(tail)
    return {"slots": slots, "dropped_slots": dropped, "main": rows, "yolo": jobs,
            "yolo_due": offers, "yolo_admitted": admitted, "yolo_busy": busy}, after


def quantiles(values):
    a = sorted(values)
    if not a:
        return None
    result = {}
    for label, fraction in (("p50", .5), ("p95", .95), ("p99", .99)):
        pos = (len(a) - 1) * fraction
        lo = int(pos)
        hi = min(lo + 1, len(a) - 1)
        result[label] = round(a[lo] + (a[hi] - a[lo]) * (pos - lo), 4)
    result["max"] = round(a[-1], 4)
    return result


def summarize(blocks):
    rows = [r for b in blocks for r in b["main"]]
    jobs = [r for b in blocks for r in b["yolo"]]
    milliseconds = lambda x: x / 1e6
    ends = [y["end_ns"] for y in jobs]
    overlaps_yolo, non_overlap = [], []
    for row in rows:
        ix = bisect_right(ends, row["start_ns"])
        if ix < len(jobs) and jobs[ix]["start_ns"] < row["reply_ns"]:
            overlaps_yolo.append(row)
        else:
            non_overlap.append(row)
    total = sum(b["slots"] for b in blocks)
    late = sum(r["reply_ns"] - r["scheduled_ns"] > PERIOD_NS for r in rows)
    dropped = sum(b["dropped_slots"] for b in blocks)
    admitted = sum(b["yolo_admitted"] for b in blocks)
    assert len(rows) + dropped == total
    assert admitted == len(jobs)
    assert all(b["offered_ns"] - a["offered_ns"] >= OFFER_NS for a, b in zip(jobs, jobs[1:]))
    return {"camera_slots": total, "processed": len(rows), "deadline_misses": late, "dropped_slots": dropped,
            "reply_from_slot_ms": quantiles(milliseconds(r["reply_ns"] - r["scheduled_ns"]) for r in rows),
            "main_service_ms": quantiles(milliseconds(r["reply_ns"] - r["start_ns"]) for r in rows),
            "main_gpu_ms": quantiles(r["gpu_ms"] for r in rows),
            "main_trt_gpu_ms": quantiles(r["trt_gpu_ms"] for r in rows),
            "yolo_offers": sum(b["yolo_due"] for b in blocks), "yolo_admitted": admitted,
            "yolo_completed": len(jobs), "yolo_busy": sum(b["yolo_busy"] for b in blocks),
            "yolo_effective_hz": round(admitted * 1e9 / (total * PERIOD_NS), 4),
            "yolo_service_ms": quantiles(milliseconds(y["end_ns"] - y["start_ns"]) for y in jobs),
            "yolo_offer_to_completion_ms": quantiles(milliseconds(y["end_ns"] - y["offered_ns"]) for y in jobs),
            "yolo_candidate_count": quantiles(y["candidates"] for y in jobs),
            "yolo_retained_count": quantiles(y["retained"] for y in jobs),
            "main_overlaps_yolo": len(overlaps_yolo),
            "overlap_main_gpu_ms": quantiles(r["gpu_ms"] for r in overlaps_yolo),
            "non_overlap_main_gpu_ms": quantiles(r["gpu_ms"] for r in non_overlap)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-plan", type=Path, required=True)
    parser.add_argument("--yolo-plan", type=Path, required=True)
    parser.add_argument("--yolo-library", type=Path, required=True)
    parser.add_argument("--cuda-library", default="libcudart.so.12")
    parser.add_argument("--frame-yuv6", type=Path, help="optional exact 196608-byte road-frame fixture")
    parser.add_argument("--slots", type=int, default=240, help="scheduled 20 Hz frames per ABBA block")
    parser.add_argument("--warm-slots", type=int, default=40)
    parser.add_argument("--worker-start-after-slot-ms", type=float, default=0,
                        help="defer YOLO until this offset from a slot, to exercise GPU overlap")
    parser.add_argument("--eager", action="store_true", help="run the main stream without CUDA graph capture")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 20 <= args.slots <= 10000 or not 8 <= args.warm_slots <= 300:
        parser.error("slots must be 20–10000, warm slots must be 8–300")
    if not 0 <= args.worker_start_after_slot_ms < 120:
        parser.error("worker start offset must be at least zero and below 120 ms")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    synthetic = bytes([64]) * (4 * FRAME_BYTES // 6) + bytes([128]) * (2 * FRAME_BYTES // 6)
    if args.frame_yuv6:
        frame = args.frame_yuv6.read_bytes()
        if len(frame) != FRAME_BYTES:
            parser.error(f"expected {FRAME_BYTES} bytes of packed YUV6")
    else:
        frame = synthetic
    engine = MainEngine(args.main_plan, args.cuda_library, synthetic)
    worker = None
    blocks = []
    try:
        for _ in range(5):
            engine.run()
        if not args.eager:
            engine.capture()
            engine.run()
        reference = engine.output_hashes()
        # One context per model throughout. A includes the resident but idle
        # auxiliary process, keeping GPU allocations invariant across modes.
        worker = YoloProcess(args.yolo_library.resolve(), args.yolo_plan, frame)
        for _ in range(8):
            worker.offer()
            worker.drain()
        after = 0
        worker_start_ns = round(args.worker_start_after_slot_ms * 1e6)
        for label in "ABBA":
            enabled = worker if label == "B" else None
            _, after = paced(engine, enabled, args.warm_slots, after, worker_start_ns)
            block, after = paced(engine, enabled, args.slots, after, worker_start_ns)
            block["mode"] = label
            block["output_hash_matches"] = engine.output_hashes() == reference
            blocks.append(block)
            print(json.dumps({"block": len(blocks), "mode": label, **summarize([block])}), flush=True)
        result = {"platform": platform.platform(), "tensorrt": engine.trt_version,
                  "main_plan": args.main_plan.name, "yolo_plan": args.yolo_plan.name,
                  "yolo_library": args.yolo_library.name, "camera_hz": 20, "max_yolo_hz": 5,
                  "cuda_graph": not args.eager, "slots_per_block": args.slots,
                  "warm_slots_per_block": args.warm_slots, "yuv6_fixture": args.frame_yuv6.name if args.frame_yuv6 else None,
                  "worker_start_after_slot_ms": args.worker_start_after_slot_ms,
                  "main_inputs": "static pinned host buffers; no recurrent state feedback",
                  "overlap_definition": "intersecting main and YOLO wall service intervals, not kernel concurrency",
                  "same_main_outputs_in_all_blocks": all(b["output_hash_matches"] for b in blocks),
                  "tensor_io": engine.metadata,
                  "modes": {k: summarize([b for b in blocks if b["mode"] == k]) for k in "AB"},
                  "blocks": blocks}
        with args.output.open("x") as out:
            json.dump(result, out, indent=2)
    finally:
        if worker is not None:
            worker.close()
        engine.close()


if __name__ == "__main__":
    main()
