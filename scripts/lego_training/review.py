"""Contact sheet of the labels that build_dataset.py would keep — check it
before training: a wrong or missing box teaches the model the wrong thing.

    python review.py FRAMES_DIR labels.json review.jpg [--min-score 0.35]

Kept boxes are green, dropped ones (score too low / above --ignore-above-y)
grey, skipped frames are marked SKIP. Frames are numbered in file order.
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import kept_boxes  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("frames")
    ap.add_argument("labels")
    ap.add_argument("out")
    ap.add_argument("--min-score", type=float, default=0.35)
    ap.add_argument("--ignore-above-y", type=float, default=0)
    ap.add_argument("--cols", type=int, default=4)
    args = ap.parse_args()

    labels = json.loads(Path(args.labels).read_text())
    names = sorted(labels)
    tw, th = 480, 360
    rows = (len(names) + args.cols - 1) // args.cols
    sheet = Image.new("RGB", (args.cols * tw, rows * th))
    for k, name in enumerate(names):
        entry = labels[name]
        im = Image.open(Path(args.frames) / name).convert("RGB")
        d = ImageDraw.Draw(im)
        keep = kept_boxes(entry, args.min_score, args.ignore_above_y)
        for b in entry.get("boxes", []):
            ok = b[:4] in keep
            d.rectangle(b[:4], outline=(0, 255, 0) if ok else (110, 110, 110), width=3 if ok else 1)
            if len(b) > 4:
                d.text((b[0], max(b[1] - 11, 0)), f"{b[4]:.2f}", fill=(0, 255, 0) if ok else (150, 150, 150))
        tag = f"#{k + 1}" + ("  SKIP" if entry.get("skip") else "")
        d.rectangle([0, 0, 110, 20], fill=(0, 0, 0))
        d.text((5, 5), tag, fill=(255, 80, 80) if entry.get("skip") else (255, 255, 255))
        sheet.paste(im.resize((tw, th)), ((k % args.cols) * tw, (k // args.cols) * th))
    sheet.save(args.out, quality=88)
    print(f"{len(names)} frames → {args.out}")


if __name__ == "__main__":
    main()
