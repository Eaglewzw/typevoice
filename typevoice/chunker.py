"""长录音分段切分器 — AudioChunker 的移植。

把一段 16kHz 录音按「语音停顿」切成多个可并行识别的片段：
- 每 10ms 一帧算 RMS 能量（30ms 窗、10ms 帧移）；用本条录音自己的
  P10(底噪)/P90(说话) 归一化，整体偏小声或带增益压缩都照样能分辨；
- 停顿门槛 = 活跃度分布的第 30 百分位（自适应，封顶 0.6，连续说话不硬切）；
- 只在「连续 ≥220ms 低于门槛」的低谷、且切在其最安静一帧下刀；
- 段数上限对齐并发上限（默认 5 段一波发完），超长录音按每段 ≤90s 增加段数。
"""

import numpy as np

SAMPLE_RATE_ASSUMED = 16000
WIN_SAMPLES = 480          # 30ms 分析窗
HOP_SAMPLES = 160          # 10ms 帧移
MIN_SPLIT_DURATION = 10.0
IDEAL_CHUNK_COUNT = 5
MIN_CHUNK_DURATION = 5.0   # 段数规划的最小段长
MAX_CHUNK_DURATION = 90.0
PAUSE_PERCENTILE = 30.0    # 停顿门槛取活跃度的此百分位
PAUSE_CAP = 0.60           # 门槛封顶（连续说话不硬切）
MIN_PAUSE_DURATION = 0.22  # 可下刀的停顿最短时长
SMOOTH_WINDOW = 5          # 活跃度平滑窗（50ms）
SNAP_RATIO = 0.4
MIN_RESULT_DURATION = 4.0  # 切出的段不得短于此，避免碎段


def percentile(sorted_arr, p: float) -> float:
    """线性插值百分位，入参需升序。"""
    if len(sorted_arr) == 0:
        return 0.0
    if len(sorted_arr) == 1:
        return float(sorted_arr[0])
    rank = p / 100.0 * (len(sorted_arr) - 1)
    lo = int(np.floor(rank))
    frac = rank - lo
    if lo + 1 >= len(sorted_arr):
        return float(sorted_arr[-1])
    return float(sorted_arr[lo] + frac * (sorted_arr[lo + 1] - sorted_arr[lo]))


def frame_energies(samples: np.ndarray) -> np.ndarray:
    """每 10ms 一帧、30ms 窗的 RMS 能量（帧重叠）。"""
    samples = np.asarray(samples, dtype=np.float32)
    n = len(samples)
    if n < WIN_SAMPLES:
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        return np.array([np.sqrt(np.mean(samples.astype(np.float64) ** 2))], dtype=np.float32)
    # 滑窗均方根：用累计和向量化（窗内平方和 / 窗长）
    sq = samples.astype(np.float64) ** 2
    cumsum = np.concatenate(([0.0], np.cumsum(sq)))
    starts = np.arange(0, n - WIN_SAMPLES + 1, HOP_SAMPLES)
    sums = cumsum[starts + WIN_SAMPLES] - cumsum[starts]
    return np.sqrt(sums / WIN_SAMPLES).astype(np.float32)


def box_smooth(a: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(a) == 0:
        return a
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(a.astype(np.float64), kernel, mode="same").astype(np.float32)


def activity_signal(samples: np.ndarray):
    """归一化+平滑的语音活跃度信号 + 自适应停顿门槛。

    返回 (activity, floor, speech, threshold)；整体音量过低（疑似无语音）返回 None。
    """
    energies = frame_energies(samples)
    if len(energies) < 10:
        return None
    sorted_e = np.sort(energies)
    floor = percentile(sorted_e, 10)
    speech = percentile(sorted_e, 90)
    if speech <= 1e-3:
        return None
    span = max(speech - floor, 1e-9)
    act = np.clip((energies - floor) / span, 0.0, 1.0).astype(np.float32)
    act = box_smooth(act, SMOOTH_WINDOW)
    threshold = min(percentile(np.sort(act), PAUSE_PERCENTILE), PAUSE_CAP)
    return act, float(floor), float(speech), float(threshold)


def _find_pauses(activity: np.ndarray, threshold: float):
    """每个合格停顿返回 (center_sample, depth, length_frames)。"""
    min_len = max(1, int(round(MIN_PAUSE_DURATION / 0.01)))
    pauses = []
    i, n = 0, len(activity)
    while i < n:
        if activity[i] < threshold:
            j = i
            while j < n and activity[j] < threshold:
                j += 1
            if j - i >= min_len:
                min_idx = i + int(np.argmin(activity[i:j]))
                center = min_idx * HOP_SAMPLES + WIN_SAMPLES // 2
                pauses.append((center, float(1.0 - activity[min_idx]), j - i))
            i = j
        else:
            i += 1
    return pauses


def planned_chunk_count(duration: float) -> int:
    base = min(int(duration / MIN_CHUNK_DURATION), IDEAL_CHUNK_COUNT)
    if base <= 1:
        return 1
    if duration / base > MAX_CHUNK_DURATION:
        return int(np.ceil(duration / MAX_CHUNK_DURATION))
    return base


def plan(samples: np.ndarray, sample_rate: int = 16000):
    """返回 [(start, end), ...] 样本区间；不分段时返回整段 [(0, len)]。"""
    n = len(samples)
    duration = n / max(sample_rate, 1)
    if duration < MIN_SPLIT_DURATION:
        return [(0, n)]
    nchunks = planned_chunk_count(duration)
    if nchunks <= 1:
        return [(0, n)]

    sig = activity_signal(samples)
    if sig is None:
        return [(0, n)]
    activity, floor, speech, threshold = sig
    pauses = _find_pauses(activity, threshold)
    if not pauses:
        return [(0, n)]

    target_len = n / nchunks
    snap = target_len * SNAP_RATIO
    min_chunk = int(MIN_RESULT_DURATION * sample_rate)

    cuts = []
    prev = 0
    for i in range(1, nchunks):
        ideal = i * target_len
        best_center = None
        best_key = None
        for center, depth, length_frames in pauses:
            if abs(center - ideal) > snap:
                continue
            if center - prev < min_chunk or n - center < min_chunk:
                continue
            key = (int(round(depth * 100)), min(length_frames, 40), -int(abs(center - ideal)))
            if best_key is None or key > best_key:
                best_key = key
                best_center = center
        if best_center is not None:
            cuts.append(best_center)
            prev = best_center
    if not cuts:
        return [(0, n)]

    ranges = []
    start = 0
    for cut in cuts:
        ranges.append((start, cut))
        start = cut
    ranges.append((start, n))
    return ranges
