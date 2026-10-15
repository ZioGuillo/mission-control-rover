"""ONNX → TFLite for the robocar (app/models/lego_minifigure.tflite).

    python convert_tflite.py best.onnx out_dir --frames FRAMES_DIR [--imgsz 416]

Why not `yolo export format=tflite`: on recent Ultralytics it goes through
the LiteRT converter, which failed here ('aten' has no attribute 'cholesky').
onnx2tf works, but it downloads sample images to check its output and that
download was broken (a pickled .npy) — so this feeds it real rover frames,
letterboxed exactly like the robot does, instead.

Writes best_float32.tflite and best_float16.tflite; ship the float16 one
(half the size, same detections). Needs its own venv — TensorFlow and the
training venv's PyTorch don't mix well: see README.md.
"""
import argparse
import glob

import numpy as np
from PIL import Image


def sample_data(frames_dir: str, size: int, n: int = 20) -> np.ndarray:
    out = []
    for f in sorted(glob.glob(f"{frames_dir}/*.jpg"))[:n]:
        im = Image.open(f).convert("RGB")
        scale = min(size / im.width, size / im.height)
        nw, nh = round(im.width * scale), round(im.height * scale)
        sq = Image.new("RGB", (size, size), (114, 114, 114))
        sq.paste(im.resize((nw, nh)), ((size - nw) // 2, (size - nh) // 2))
        out.append(np.asarray(sq, dtype=np.float32) / 255.0)
    if not out:
        raise SystemExit(f"no .jpg frames in {frames_dir}")
    return np.stack(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("onnx")
    ap.add_argument("out_dir")
    ap.add_argument("--frames", required=True, help="rover frames used as conversion sample data")
    ap.add_argument("--imgsz", type=int, default=416)
    args = ap.parse_args()

    import onnx2tf
    import onnx2tf.onnx2tf as core
    from onnx2tf.utils import common_functions

    data = sample_data(args.frames, args.imgsz)
    common_functions.download_test_image_data = lambda: data
    core.download_test_image_data = lambda: data
    onnx2tf.convert(input_onnx_file_path=args.onnx, output_folder_path=args.out_dir,
                    output_signaturedefs=True, not_use_onnxsim=True)


if __name__ == "__main__":
    main()
