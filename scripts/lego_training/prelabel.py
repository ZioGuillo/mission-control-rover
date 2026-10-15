"""Pre-label rover frames with OWLv2 (open-vocabulary detection).

Draws candidate minifigure boxes so labelling is mostly checking, not drawing:

    python prelabel.py FRAMES_DIR labels.json
    python review.py FRAMES_DIR labels.json review.jpg   # then look at it

labels.json maps each frame to its boxes, [x1, y1, x2, y2, score] in pixels.
Edit it by hand to fix mistakes before build_dataset.py:
  - delete a wrong box
  - add a missed one as [x1, y1, x2, y2] (no score = always kept)
  - add "skip": true to leave a frame out (e.g. figures hidden by a hand)
Re-running keeps frames already in labels.json, so manual edits survive.
"""
import argparse
import glob
import json
from pathlib import Path

PROMPTS = ["a lego minifigure", "a small lego toy figure"]
MODEL = "google/owlv2-base-patch16-ensemble"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("frames", help="folder of .jpg frames (unzipped training_frames_*.zip)")
    ap.add_argument("labels", help="labels.json to create or extend")
    ap.add_argument("--threshold", type=float, default=0.15, help="lowest OWLv2 score kept")
    args = ap.parse_args()

    import torch
    from PIL import Image
    from transformers import Owlv2ForObjectDetection, Owlv2Processor

    out_path = Path(args.labels)
    labels = json.loads(out_path.read_text()) if out_path.exists() else {}
    todo = [f for f in sorted(glob.glob(f"{args.frames}/*.jpg")) if Path(f).name not in labels]
    if not todo:
        print("every frame is already in", out_path)
        return

    proc = Owlv2Processor.from_pretrained(MODEL)
    model = Owlv2ForObjectDetection.from_pretrained(MODEL).eval()
    for f in todo:
        img = Image.open(f).convert("RGB")
        inputs = proc(text=[PROMPTS], images=img, return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
        # OWLv2 pads the image to a square, so boxes are in that square's pixels
        side = max(img.size)
        r = proc.post_process_grounded_object_detection(
            outputs, threshold=args.threshold, target_sizes=[(side, side)])[0]
        boxes = sorted(
            ([round(float(v), 1) for v in b] + [round(float(s), 3)] for b, s in zip(r["boxes"], r["scores"])),
            key=lambda b: -b[4],
        )
        labels[Path(f).name] = {"boxes": boxes[:4]}
        print(Path(f).name, boxes[:2], flush=True)
    out_path.write_text(json.dumps(labels, indent=1))
    print(f"{len(todo)} frames labelled → {out_path}")


if __name__ == "__main__":
    main()
