# Models

Drop trained models here. The code looks for:

- `payload_classifier.tflite` — image classifier, **package vs munition**
  (output index order must match `config.VISION_LABELS`).

## Fast way to make the payload classifier
1. Go to **Google Teachable Machine** → *Image Project*.
2. Two classes: `package` and `munition`. Hold each mock payload to the webcam
   and capture ~150–300 samples per class at different angles/lighting.
3. Export → **TensorFlow Lite → Floating point** → save the `.tflite` here as
   `payload_classifier.tflite`.
4. Set `MOCK_VISION = False` in `config.py`.

Same recipe with an **Audio Project** if you later want the acoustic classifier
to run on the Uno Q's webcam mic instead of the car's mic features.

Until a model is present, the code runs in MOCK vision mode automatically.
