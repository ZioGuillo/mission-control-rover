"""Score a model on the dataset's real held-out rover frames — the number
that matters (Ultralytics' own mAP mixes in the easy product photos).

    python evaluate.py runs/detect/lego/weights/best.pt dataset [--imgsz 416]

A labelled minifigure counts as found when a box with conf ≥ --conf overlaps
it by IoU ≥ 0.5; every other box is a false one. Writes eval.jpg (green =
label, magenta = model) next to the dataset.
"""
import argparse
import glob
from pathlib import Path

from PIL import Image, ImageDraw


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def match(gt: list, preds: list, min_iou: float = 0.5) -> tuple[list, list]:
    """Greedy matching. Returns (score or None per label, unmatched scores)."""
    used, hits = set(), []
    for g in gt:
        cands = [i for i in range(len(preds)) if i not in used]
        best = max(cands, key=lambda i: iou(g, preds[i][0]), default=None)
        if best is not None and iou(g, preds[best][0]) >= min_iou:
            used.add(best)
            hits.append(preds[best][1])
        else:
            hits.append(None)
    return hits, [c for i, (_, c) in enumerate(preds) if i not in used]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model")
    ap.add_argument("dataset")
    ap.add_argument("--imgsz", type=int, default=416)
    ap.add_argument("--conf", type=float, default=0.3)
    args = ap.parse_args()

    from ultralytics import YOLO

    model = YOLO(args.model)
    ds = Path(args.dataset)
    found = total = false = 0
    tiles = []
    for img in sorted(glob.glob(str(ds / "images/val/real*.jpg"))):
        W, H = Image.open(img).size
        gt = []
        for line in Path(img.replace("/images/", "/labels/")).with_suffix(".txt").read_text().splitlines():
            _, cx, cy, w, h = map(float, line.split())
            gt.append([(cx - w / 2) * W, (cy - h / 2) * H, (cx + w / 2) * W, (cy + h / 2) * H])
        r = model.predict(img, imgsz=args.imgsz, conf=args.conf, verbose=False)[0]
        preds = [(b.tolist(), float(c)) for b, c in zip(r.boxes.xyxy, r.boxes.conf)]
        hits, extra = match(gt, preds)
        found += sum(h is not None for h in hits)
        total += len(gt)
        false += len(extra)
        print(f"{Path(img).stem}: minifigures {[round(h, 2) if h else 'miss' for h in hits]}"
              f"  false boxes {[round(c, 2) for c in extra]}")
        im = Image.open(img).convert("RGB")
        d = ImageDraw.Draw(im)
        for g in gt:
            d.rectangle(g, outline=(0, 255, 0), width=2)
        for b, c in preds:
            d.rectangle(b, outline=(255, 0, 255), width=2)
            d.text((b[0], max(b[1] - 11, 0)), f"{c:.2f}", fill=(255, 0, 255))
        tiles.append(im.resize((480, 360)))
    print(f"\nfound {found}/{total} minifigures, {false} false boxes")
    if tiles:
        cols = 3
        sheet = Image.new("RGB", (cols * 480, ((len(tiles) + cols - 1) // cols) * 360))
        for k, t in enumerate(tiles):
            sheet.paste(t, ((k % cols) * 480, (k // cols) * 360))
        sheet.save(ds / "eval.jpg", quality=88)
        print("→", ds / "eval.jpg")


if __name__ == "__main__":
    main()
