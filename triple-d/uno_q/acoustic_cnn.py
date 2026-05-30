"""acoustic_cnn.py - DETECT stage, CNN flavor (Uno Q's own mic).

Drop-in replacement for detect.AcousticDetector. Instead of gating on the
amp/pitch features the UNO R3 extracted, this listens on the Uno Q's own
microphone in constant sliding windows, turns each window into a Mel-
spectrogram, and runs the trained DroneDetectorCNN (drone_acoustic_cnn.pth).

It exposes the EXACT same interface main.py expects:
    update(telem) -> bool      # returns True while a contact is held
    .detected : bool
    .confidence : float        # P(threat), feeds decide.assess()

`telem` is ignored here (audio comes from the local mic), so the rest of the
pipeline -- DECIDE fusion, the closing tracker, operator gating -- is unchanged.

Design: a background thread continuously fills a 2.0s ring buffer from the mic
and a second thread runs inference at its own cadence, so a slow forward pass
never stalls main.py's 20 Hz control loop. update() just reads the latest
cached verdict.

Requires: torch, librosa, sounddevice, numpy  (see requirements.txt).
"""
import threading
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import librosa
# sounddevice is imported lazily (only for the live-mic source) so the WAV
# source + offline tests run without PortAudio installed.

import config


# ---------------------------------------------------------------------------
# Preprocessing constants -- MUST match the training config of best_model.pth
# (SudarshanChakra configs/config.py). Vendored here so triple-d stays
# self-contained on the Uno Q.
# ---------------------------------------------------------------------------
SAMPLE_RATE = 22050
DURATION    = 2.0
N_SAMPLES   = int(SAMPLE_RATE * DURATION)
N_FFT       = 2048
HOP_LENGTH  = 512
N_MELS      = 128
F_MIN       = 20
F_MAX       = 8000
THREAT_LABEL = 1          # index order from training: 0=Safe, 1=Threat


# ---------------------------------------------------------------------------
# Model architecture -- vendored from SudarshanChakra src/model.py
# (custom_cnn / DroneDetectorCNN). Keep in sync if you retrain a new arch.
# ---------------------------------------------------------------------------
class _ConvBlock(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_size=3, stride=1, padding=1, pool=2):
        super().__init__()
        self.conv = nn.Conv2d(in_ch, out_ch, kernel_size, stride, padding, bias=False)
        self.bn = nn.BatchNorm2d(out_ch)
        self.pool = nn.MaxPool2d(pool, pool)

    def forward(self, x):
        return self.pool(F.relu(self.bn(self.conv(x)), inplace=True))


class DroneDetectorCNN(nn.Module):
    def __init__(self, input_channels=1, num_classes=2, dropout_rate=0.5):
        super().__init__()
        self.conv1 = _ConvBlock(input_channels, 32)
        self.conv2 = _ConvBlock(32, 64)
        self.conv3 = _ConvBlock(64, 128)
        self.conv4 = _ConvBlock(128, 256)
        self.global_pool = nn.AdaptiveAvgPool2d((1, 1))
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(256, 128), nn.ReLU(inplace=True), nn.Dropout(dropout_rate),
            nn.Linear(128, 64),  nn.ReLU(inplace=True), nn.Dropout(dropout_rate),
            nn.Linear(64, num_classes),
        )

    def forward(self, x):
        x = self.conv4(self.conv3(self.conv2(self.conv1(x))))
        x = self.global_pool(x)
        return self.classifier(x)


def _to_mel(waveform: np.ndarray) -> np.ndarray:
    """Waveform -> log-Mel, per-clip min-max normalized. Mirrors training."""
    mel = librosa.feature.melspectrogram(
        y=waveform, sr=SAMPLE_RATE, n_fft=N_FFT, hop_length=HOP_LENGTH,
        n_mels=N_MELS, fmin=F_MIN, fmax=F_MAX, power=2.0,
    )
    mel_db = librosa.power_to_db(mel, ref=np.max)
    return (mel_db - mel_db.min()) / (mel_db.max() - mel_db.min() + 1e-8)


def _load_cnn(model_path: str, device: torch.device) -> nn.Module:
    """Build the arch and load trained weights (matches inference.py)."""
    model = DroneDetectorCNN()
    ck = torch.load(model_path, map_location=device, weights_only=False)
    model.load_state_dict(ck["model_state_dict"])
    return model.eval().to(device)


@torch.no_grad()
def classify_window(model: nn.Module, device: torch.device,
                    window: np.ndarray) -> float:
    """One 2.0s window (any length; padded/truncated) -> P(threat)."""
    if len(window) < N_SAMPLES:
        window = np.pad(window, (0, N_SAMPLES - len(window)))
    elif len(window) > N_SAMPLES:
        window = window[:N_SAMPLES]
    mel = _to_mel(window.astype(np.float32))
    x = torch.from_numpy(mel).float().unsqueeze(0).unsqueeze(0).to(device)
    probs = F.softmax(model(x), dim=1)
    return float(probs[0, THREAT_LABEL].item())


class CnnAcousticDetector:
    """Sliding-window CNN drone detector. Drop-in for AcousticDetector."""

    def __init__(self):
        self.detected = False
        self.confidence = 0.0
        self._since = None                 # when threat first appeared (debounce)
        self._lock = threading.Lock()
        self._running = True

        # Ring buffer of the most recent N_SAMPLES of audio (mono float32).
        self._buf = np.zeros(N_SAMPLES, dtype=np.float32)

        # Load model.
        self.device = torch.device("cpu")  # Uno Q has no CUDA; CPU is fine
        self.model = _load_cnn(config.ACOUSTIC_MODEL_PATH, self.device)
        print(f"[detect] CNN acoustic model loaded ({config.ACOUSTIC_MODEL_PATH})")

        # Audio source: a looped WAV (offline testing) or the live mic.
        self._stream = None
        wav = getattr(config, "ACOUSTIC_SOURCE_WAV", "") or ""
        if wav:
            clip, _ = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
            self._wav = clip.astype(np.float32)
            threading.Thread(target=self._wav_feed, daemon=True).start()
            print(f"[detect] audio source = LOOPED WAV ({wav}) -- offline test mode")
        else:
            import sounddevice as sd
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=int(SAMPLE_RATE * 0.1), callback=self._on_audio,
            )
            self._stream.start()
            print("[detect] audio source = live mic")

        # Inference thread (sliding-window classify -> cached verdict).
        self._infer_thread = threading.Thread(target=self._infer_loop, daemon=True)
        self._infer_thread.start()
        print("[detect] sliding-window inference running")

    # --- live mic: shift new samples into the ring buffer ------------------
    def _on_audio(self, indata, frames, time_info, status):  # noqa: ARG002
        self._push(indata[:, 0])

    # --- WAV source: stream the file into the ring buffer in real time -----
    def _wav_feed(self):
        step = int(SAMPLE_RATE * 0.1)            # 0.1s chunks, like the mic
        i = 0
        while self._running:
            chunk = self._wav[i:i + step]
            if len(chunk) < step:                # loop back to the start
                i = 0
                continue
            self._push(chunk)
            i += step
            time.sleep(0.1)

    def _push(self, chunk: np.ndarray):
        n = len(chunk)
        if n >= N_SAMPLES:
            self._buf[:] = chunk[-N_SAMPLES:]
        else:
            self._buf[:-n] = self._buf[n:]
            self._buf[-n:] = chunk

    # --- inference loop: classify the latest window, apply hold+threshold ---
    def _infer_loop(self):
        while self._running:
            t0 = time.time()
            window = self._buf.copy()
            prob_threat = self._classify(window)
            self._apply(prob_threat)
            # cadence ~ INFER_PERIOD; never busier than that
            time.sleep(max(0.0, config.ACOUSTIC_INFER_PERIOD - (time.time() - t0)))

    def _classify(self, window: np.ndarray) -> float:
        return classify_window(self.model, self.device, window)

    def _apply(self, prob_threat: float):
        """Threshold + DETECT_HOLD debounce, same semantics as detect.py."""
        now = time.time()
        signature = prob_threat >= config.ACOUSTIC_THRESHOLD
        with self._lock:
            if signature:
                if self._since is None:
                    self._since = now
                held = now - self._since
                self.detected = held >= config.DETECT_HOLD
                self.confidence = round(prob_threat, 2)
            else:
                self._since = None
                self.detected = False
                self.confidence = 0.0

    # --- interface main.py calls -------------------------------------------
    def update(self, telem: dict) -> bool:        # noqa: ARG002 (telem unused)
        with self._lock:
            return self.detected

    def close(self):
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:                      # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Standalone self-test (no full pipeline needed):
#   python acoustic_cnn.py --wav path/to/clip.wav   # one-shot, no mic/threads
#   python acoustic_cnn.py                           # live mic, prints P(threat)
# ---------------------------------------------------------------------------
def _selftest():
    import argparse
    ap = argparse.ArgumentParser(description="CNN acoustic detector self-test")
    ap.add_argument("--wav", help="classify one WAV file and exit (no mic)")
    ap.add_argument("--model", default=config.ACOUSTIC_MODEL_PATH)
    args = ap.parse_args()

    device = torch.device("cpu")
    model = _load_cnn(args.model, device)
    thr = config.ACOUSTIC_THRESHOLD

    if args.wav:
        clip, _ = librosa.load(args.wav, sr=SAMPLE_RATE, mono=True)
        p = classify_window(model, device, clip)
        verdict = "THREAT" if p >= thr else "safe"
        print(f"P(threat)={p:.3f}  thr={thr}  -> {verdict}   [{args.wav}]")
        return

    import sounddevice as sd
    buf = np.zeros(N_SAMPLES, dtype=np.float32)

    def cb(indata, frames, t, status):            # noqa: ARG001
        c = indata[:, 0]; n = len(c)
        buf[:-n] = buf[n:]; buf[-n:] = c

    print("Listening (Ctrl+C to stop)...")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=int(SAMPLE_RATE * 0.1), callback=cb):
        try:
            while True:
                p = classify_window(model, device, buf.copy())
                bar = "#" * int(p * 40)
                print(f"\rP(threat)={p:.3f} |{bar:<40}| "
                      f"{'THREAT' if p >= thr else '      '}", end="", flush=True)
                time.sleep(config.ACOUSTIC_INFER_PERIOD)
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    _selftest()
