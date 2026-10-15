"""TensorRT inference worker — runs on the Jetson's GPU.

Started by app/hardware/trt_client.py under the *system* Python
(/usr/bin/python3): JetPack ships TensorRT's Python bindings only for its
own Python 3.6, not for the app's venv. So this file must stay Python 3.6
compatible and import nothing from the app — only tensorrt, numpy, ctypes.

CUDA memory is handled through ctypes on libcudart directly, so there's no
pycuda to compile.

Protocol (binary, over stdin/stdout):
  startup   one JSON line: {"ok": true, "engines": [{"input": [1,3,S,S],
            "output": [...]}, ...]}   or {"ok": false, "error": "..."}
  request   uint32 engine index, uint32 byte count, then an RGB uint8
            image of shape (S, S, 3) — already letterboxed by the app
  response  uint32 status (0 = ok), uint32 byte count, then the float32
            output (shape from the startup line) — or a UTF-8 error message
"""
import ctypes
import json
import os
import struct
import sys

HEADER = struct.Struct("<II")
_H2D, _D2H = 1, 2


def _cuda():
    for name in ("libcudart.so", "/usr/local/cuda/lib64/libcudart.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OSError("libcudart.so not found")


class Engine(object):
    def __init__(self, trt, runtime, cuda, path):
        with open(path, "rb") as f:
            self.engine = runtime.deserialize_cuda_engine(f.read())
        if self.engine is None:
            raise RuntimeError("could not load engine %s (built for another GPU/TensorRT?)" % path)
        self.context = self.engine.create_execution_context()
        self.cuda = cuda
        self.input_shape = self.output_shape = None
        self.bindings = []
        for i in range(self.engine.num_bindings):
            shape = tuple(self.engine.get_binding_shape(i))
            size = 4
            for d in shape:
                size *= d
            ptr = ctypes.c_void_p()
            if cuda.cudaMalloc(ctypes.byref(ptr), ctypes.c_size_t(size)) != 0:
                raise RuntimeError("cudaMalloc failed")
            self.bindings.append(ptr.value)
            if self.engine.binding_is_input(i):
                self.input_shape, self.input_ptr, self.input_size = shape, ptr.value, size
            else:
                self.output_shape, self.output_ptr, self.output_size = shape, ptr.value, size

    def run(self, image, np):
        size = self.input_shape[2]
        img = np.frombuffer(image, dtype=np.uint8).reshape(size, size, 3)
        # HWC uint8 0-255 → NCHW float32 0-1, contiguous for the copy
        x = np.ascontiguousarray(img.transpose(2, 0, 1)[np.newaxis], dtype=np.float32)
        x /= 255.0
        self.cuda.cudaMemcpy(ctypes.c_void_p(self.input_ptr), x.ctypes.data_as(ctypes.c_void_p),
                             ctypes.c_size_t(self.input_size), _H2D)
        if not self.context.execute_v2(self.bindings):
            raise RuntimeError("TensorRT execution failed")
        out = np.empty(self.output_shape, dtype=np.float32)
        self.cuda.cudaMemcpy(out.ctypes.data_as(ctypes.c_void_p), ctypes.c_void_p(self.output_ptr),
                             ctypes.c_size_t(self.output_size), _D2H)
        return out.tobytes()


def _read_exact(stream, n):
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def main():
    # Keep a private handle on the real stdout for the protocol, and point
    # fd 1 at stderr so nothing TensorRT prints can corrupt the stream.
    out = os.fdopen(os.dup(1), "wb", 0)
    os.dup2(2, 1)
    inp = os.fdopen(0, "rb", 0)
    try:
        import numpy as np
        import tensorrt as trt

        logger = trt.Logger(trt.Logger.WARNING)
        runtime = trt.Runtime(logger)
        cuda = _cuda()
        engines = [Engine(trt, runtime, cuda, p) for p in sys.argv[1:]]
    except Exception as exc:
        out.write((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
        return 1
    out.write((json.dumps({"ok": True, "engines": [
        {"input": list(e.input_shape), "output": list(e.output_shape)} for e in engines
    ]}) + "\n").encode())

    while True:
        header = _read_exact(inp, HEADER.size)
        if header is None:
            return 0  # app closed the pipe
        index, n = HEADER.unpack(header)
        payload = _read_exact(inp, n)
        if payload is None:
            return 0
        try:
            result, status = engines[index].run(payload, np), 0
        except Exception as exc:
            result, status = str(exc).encode(), 1
        out.write(HEADER.pack(status, len(result)) + result)


if __name__ == "__main__":
    sys.exit(main())
