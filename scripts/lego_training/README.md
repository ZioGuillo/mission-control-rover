# Retraining the Lego minifigure detector

The detector in `app/models/lego_minifigure.tflite` is a YOLOv8n trained on
the [Roboflow "Lego Minifigures" set](https://universe.roboflow.com/vc-echpj/lego-minifigures-r3zzt)
(product photos, CC BY 4.0) **plus frames from the rover's own camera**.
The product photos alone don't work in a real room: every minifigure in
them fills the picture, so the model learned "a big box over everything".
On real rover frames it scored 0.01–0.04 and found nothing.

| Model | Held-out real frames |
| ----- | -------------------- |
| Roboflow photos only (320 px) | 0 / 10 minifigures |
| + 13 rover frames + synthetic pastes (416 px), 2026-10-15 v1 | 8 / 10, but 7 false boxes on 8 empty frames |
| v2: + 26 negative frames, pasted figures cut out of their backdrop | **9 / 10, 0 false boxes** |

v1 was only tested on frames that contained a minifigure, so its false
positives (small light objects, a box on the dresser — "small light
rectangle" was what its pasted, backdrop-included figures taught it) only
showed up live. Always hold out some empty frames too. v2's labels:
`labels_2026-10-15_v2.json` (46 frames: 20 with figures, 26 negatives);
test frames `--val-frames 3,7,11,13,18,20,22,25,29,33,36,39,42,45`.

It still misses large close-ups held right in front of the lens — save more
of those next time.

## 1. Collect frames (on the Jetson)

Camera tab → **📸 Save training frame**. Vary it: different minifigures,
distances (in hand, on the desk, across the room), positions in the frame,
day and night light — and ~15% with no minifigure at all. Then
**⬇ Download zip** and unzip it. **🗑 Clear** empties the robot's folder
(`~/.local/share/robocar/training_frames`) once you have the zip.

The frames can show people; keep them out of git and out of public
datasets.

## 2. Label

```bash
cd scripts/lego_training
python3.11 -m venv .venv-train && .venv-train/bin/pip install -r requirements-train.txt
.venv-train/bin/python prelabel.py ~/Downloads/training_frames_X labels.json
.venv-train/bin/python review.py ~/Downloads/training_frames_X labels.json review.jpg
```

Open `review.jpg` and fix `labels.json` by hand (the docstring in
`prelabel.py` has the format): delete wrong boxes, add missed ones as
`[x1, y1, x2, y2]`, or `"skip": true` a frame you can't label reliably.
Re-run `review.py` until every minifigure has a green box and nothing else
does. A wrong box teaches the model the wrong thing — worse than no frame.

To add to an earlier batch, put both batches' frames in one folder and
start from the earlier labels file (e.g. `labels_2026-10-15.json` for the
first 20 frames) — `prelabel.py` only labels frames it hasn't seen.

## 3. Build the dataset and train

Download the Roboflow set once: dataset page → **Download Dataset** →
format **YOLOv8** → zip.

```bash
.venv-train/bin/python build_dataset.py --frames ~/Downloads/training_frames_X \
    --labels labels.json --roboflow ~/Downloads/"Lego Minifigures.v1i.yolov8.zip" \
    --out dataset --ignore-above-y 150     # 150: the shelf band in this room

.venv-train/bin/yolo detect train model=yolov8n.pt data=dataset/data.yaml \
    imgsz=416 epochs=80 patience=25 batch=16 device=mps project=runs name=lego
```

`device=mps` is the Mac's GPU (~20 s per epoch, ~25 min); use `device=0` on
CUDA. `--val-frames 3,7,11` picks the held-out frames by their number on
`review.jpg` — keep it varied (close, far, several figures).

## 4. Check it

```bash
.venv-train/bin/python evaluate.py runs/lego/weights/best.pt dataset
```

Only ship a model that beats the current one on the same held-out frames
(see the table above), and look at `dataset/eval.jpg`.

## 5. Export and deploy

```bash
.venv-train/bin/yolo export model=runs/lego/weights/best.pt format=onnx imgsz=416 simplify=True opset=12

python3.11 -m venv .venv-convert && .venv-convert/bin/pip install -r requirements-convert.txt
.venv-convert/bin/python convert_tflite.py runs/lego/weights/best.onnx tflite_out \
    --frames ~/Downloads/training_frames_X

cp tflite_out/best_float16.tflite ../../app/models/lego_minifigure.tflite
```

Commit, then on the robot pull and restart. `app/hardware/ml_driver.py`
reads the input size and layout from the model and accepts pixel or 0–1
box coordinates, so a 416 or 320 px model drops in without code changes.
Keep `best.onnx` — it's the input for a TensorRT (GPU) engine on the Jetson.

**Don't run `yolo export format=tflite`** in the training venv: it
auto-installs `litert-torch`, which downgrades PyTorch and leaves
torchvision mismatched (`operator torchvision::nms does not exist`). If that
happened: `.venv-train/bin/pip install --upgrade torch torchvision`.

`train_lego_detector.ipynb` (one directory up) is the original Colab
version: Roboflow photos only, so it reproduces the old model, not this one.
