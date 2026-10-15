"""Build a YOLO dataset from rover frames + the Roboflow minifigure set.

    python build_dataset.py --frames FRAMES_DIR --labels labels.json \\
        --roboflow "Lego Minifigures.v1i.yolov8.zip" --out dataset \\
        --val-frames 3,7,11,13,18,20

Three ingredients:
  real       the rover's own frames (labels.json), repeated --repeat times so
             a few hundred product photos don't drown them out
  roboflow   close-up product photos — the original training set
  synthetic  minifigures cut out of the Roboflow photos (backdrop removed),
             shrunk to the sizes they appear at in a room (15–90 px) and
             pasted into the rover's training frames. Teaches "small
             minifigure in a real room", which the product photos never show.

Frames with no minifigure (an empty "boxes" list) are negatives: they
teach what isn't one — collect them where the model gets fooled.

Validation frames (--val-frames: numbers as shown by review.py, or
--val-fraction) are never trained on and never used as paste backgrounds,
so evaluate.py on them is an honest score.
"""
import argparse
import glob
import json
import random
import shutil
import tempfile
import zipfile
from pathlib import Path

from PIL import Image


def kept_boxes(entry: dict, min_score: float, ignore_above_y: float = 0) -> list[list[float]]:
    """Boxes of one labels.json entry that go into the dataset: hand-added
    ones (no score) always; OWLv2 ones scoring min_score or more. Boxes
    centred above ignore_above_y are dropped — e.g. a shelf of tiny,
    ambiguous figures that would be unfair to count either way."""
    if entry.get("skip"):
        return []
    keep = []
    for b in entry.get("boxes", []):
        if len(b) > 4 and b[4] < min_score:
            continue
        if (b[1] + b[3]) / 2 <= ignore_above_y:
            continue
        keep.append([float(v) for v in b[:4]])
    return keep


def yolo_line(box: list[float], w: int, h: int) -> str:
    x1, y1, x2, y2 = box
    return f"0 {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"


def cutout(crop: Image.Image, tolerance: int = 40) -> Image.Image:
    """Mask for a minifigure crop from a product photo: transparent where the
    pixel is close to the background colour (the median of the crop's edge
    pixels), so the pasted figure doesn't bring its white studio backdrop.
    Pasting whole rectangles taught the model that any small light
    rectangle is a minifigure."""
    import numpy as np
    from PIL import ImageFilter

    a = np.asarray(crop.convert("RGB"), dtype=np.int16)
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(edge, axis=0)
    dist = np.abs(a - bg).max(axis=2)
    mask = Image.fromarray(((dist > tolerance) * 255).astype("uint8"))
    # close pinholes, then soften the edge so the paste doesn't look cut out
    return mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1))


def _overlaps(b, others) -> bool:
    return any(not (b[2] < o[0] or b[0] > o[2] or b[3] < o[1] or b[1] > o[3]) for o in others)


def _roboflow_dir(path: Path, tmp: Path) -> Path:
    if path.is_dir():
        return path
    with zipfile.ZipFile(path) as zf:
        zf.extractall(tmp)
    return tmp


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--roboflow", required=True, help="Roboflow YOLOv8 export: .zip or unzipped folder")
    ap.add_argument("--out", required=True)
    val = ap.add_mutually_exclusive_group()
    val.add_argument("--val-frames", help="comma-separated frame numbers (1-based, as in review.py)")
    val.add_argument("--val-fraction", type=float, default=0.3)
    ap.add_argument("--min-score", type=float, default=0.35)
    ap.add_argument("--ignore-above-y", type=float, default=0)
    ap.add_argument("--repeat", type=int, default=4, help="copies of each real training frame")
    ap.add_argument("--synthetic", type=int, default=400, help="pasted-minifigure frames to make")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-cutout", dest="cutout", action="store_false",
                    help="paste whole crops, backdrop included (the 2026-10-15 v1 model)")
    args = ap.parse_args()

    random.seed(args.seed)
    labels = json.loads(Path(args.labels).read_text())
    names = [n for n in sorted(labels) if not labels[n].get("skip")]
    all_names = sorted(labels)
    if args.val_frames:
        val_names = {all_names[int(k) - 1] for k in args.val_frames.split(",")}
    else:
        val_names = set(random.sample(names, max(1, round(len(names) * args.val_fraction))))

    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    for d in ("images/train", "images/val", "labels/train", "labels/val"):
        (out / d).mkdir(parents=True)

    def write(img: Image.Image, boxes, split: str, stem: str) -> None:
        img.save(out / f"images/{split}/{stem}.jpg", quality=92)
        (out / f"labels/{split}/{stem}.txt").write_text(
            "".join(yolo_line(b, *img.size) + "\n" for b in boxes))

    train_real = []
    for k, name in enumerate(all_names, 1):
        if name not in names:
            continue
        img = Image.open(Path(args.frames) / name).convert("RGB")
        boxes = kept_boxes(labels[name], args.min_score, args.ignore_above_y)
        if name in val_names:
            write(img, boxes, "val", f"real{k:03d}")
        else:
            train_real.append((img, boxes))
            for rep in range(args.repeat):
                write(img, boxes, "train", f"real{k:03d}_{rep}")

    with tempfile.TemporaryDirectory() as tmp:
        rf = _roboflow_dir(Path(args.roboflow), Path(tmp))
        crops = []
        # Roboflow's own valid split stays validation; train + test train
        for src, dst in (("train", "train"), ("test", "train"), ("valid", "val")):
            for img_path in glob.glob(str(rf / src / "images" / "*")):
                stem = Path(img_path).stem
                shutil.copy(img_path, out / f"images/{dst}/rf_{stem}.jpg")
                label = rf / src / "labels" / f"{stem}.txt"
                shutil.copy(label, out / f"labels/{dst}/rf_{stem}.txt")
                if src != "train":
                    continue
                im = Image.open(img_path).convert("RGB")
                iw, ih = im.size
                for line in label.read_text().splitlines():
                    _, cx, cy, w, h = map(float, line.split())
                    crops.append(im.crop((int((cx - w / 2) * iw), int((cy - h / 2) * ih),
                                          int((cx + w / 2) * iw), int((cy + h / 2) * ih))))

    for i in range(args.synthetic if train_real and crops else 0):
        bg, boxes = random.choice(train_real)
        bg, boxes = bg.copy(), list(boxes)
        W, H = bg.size
        for _ in range(random.randint(1, 3)):
            c = random.choice(crops)
            th = random.randint(15, 90)
            tw = max(6, int(c.width * th / c.height))
            for _try in range(20):
                x, y = random.randint(0, W - tw), random.randint(int(H * 0.3), H - th)
                b = [x, y, x + tw, y + th]
                if not _overlaps(b, boxes):
                    piece = c.resize((tw, th))
                    bg.paste(piece, (x, y), cutout(piece) if args.cutout else None)
                    boxes.append(b)
                    break
        write(bg, boxes, "train", f"syn{i:04d}")

    (out / "data.yaml").write_text(
        f"path: {out.resolve()}\ntrain: images/train\nval: images/val\nnames: ['lego_minifigure']\n")
    print(f"train {len(list((out / 'images/train').glob('*.jpg')))}  "
          f"val {len(list((out / 'images/val').glob('*.jpg')))}  "
          f"(real val frames: {len(val_names)}) → {out / 'data.yaml'}")


if __name__ == "__main__":
    main()
