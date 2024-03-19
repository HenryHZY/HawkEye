"""
Modified from https://github.com/m-bain/frozen-in-time/blob/22a91d78405ec6032fdf521ae1ff5573358e632f/base/base_dataset.py
"""
import random
import os
import io
import av
import cv2
import decord
import imageio
from decord import VideoReader
import torch
import numpy as np
import math
decord.bridge.set_bridge("torch")

import logging
logger = logging.getLogger(__name__)

def pts_to_secs(pts: int, time_base: float, start_pts: int) -> float:
    """
    Converts a present time with the given time base and start_pts offset to seconds.

    Returns:
        time_in_seconds (float): The corresponding time in seconds.

    https://github.com/facebookresearch/pytorchvideo/blob/main/pytorchvideo/data/utils.py#L54-L64
    """
    if pts == math.inf:
        return math.inf

    return int(pts - start_pts) * time_base


def get_pyav_video_duration(video_reader):
    video_stream = video_reader.streams.video[0]
    video_duration = pts_to_secs(
        video_stream.duration,
        video_stream.time_base,
        video_stream.start_time
    )
    return float(video_duration)


def get_frame_indices_by_fps():
    pass


def get_frame_indices(num_frames, vlen, start_pos=0, end_pos=1, sample='rand', fix_start=None, input_fps=1, max_num_frames=-1):
    assert 0 <= start_pos <= 1, "start_pos must be in [0, 1]"
    assert 0 <= end_pos <= 1, "end_pos must be in [0, 1]"

    start_frame, end_frame = round(start_pos * vlen), round(end_pos * vlen)

    if sample in ["rand", "middle"]: # uniform sampling
        acc_samples = min(num_frames, vlen)
        # split the video into `acc_samples` intervals, and sample from each interval.
        intervals = np.linspace(start=start_frame, stop=end_frame, num=acc_samples + 1).astype(int)
        ranges = []
        for idx, interv in enumerate(intervals[:-1]):
            ranges.append((interv, intervals[idx + 1] - 1))
        if sample == 'rand':
            try:
                frame_indices = [random.choice(range(x[0], x[1])) for x in ranges]
            except:
                frame_indices = np.random.permutation(vlen)[:acc_samples]
                frame_indices.sort()
                frame_indices = list(frame_indices)
        elif fix_start is not None:
            frame_indices = [x[0] + fix_start for x in ranges]
        elif sample == 'middle':
            frame_indices = [(x[0] + x[1]) // 2 for x in ranges]
        else:
            raise NotImplementedError

        if len(frame_indices) < num_frames:  # padded with last frame
            padded_frame_indices = [frame_indices[-1]] * num_frames
            padded_frame_indices[:len(frame_indices)] = frame_indices
            frame_indices = padded_frame_indices

    elif "fps" in sample:  # fps0.5, sequentially sample frames at 0.5 fps
        output_fps = float(sample[3:])
        duration = float(end_frame - start_frame) / input_fps
        delta = 1 / output_fps  # gap between frames, this is also the clip length each frame represents
        frame_seconds = np.arange(start_frame + delta / 2, end_frame + delta / 2, delta)
        frame_indices = np.around(frame_seconds * input_fps).astype(int)
        frame_indices = [e for e in frame_indices if start_frame < e < end_frame]
        if max_num_frames > 0 and len(frame_indices) > max_num_frames:
            frame_indices = frame_indices[:max_num_frames]
            # frame_indices = np.linspace(0 + delta / 2, duration + delta / 2, endpoint=False, num=max_num_frames)
    else:
        raise ValueError
    return frame_indices


def read_frames_av(video_path, num_frames, sample='rand', fix_start=None, max_num_frames=-1, clip=None, client=None, fps=None):
    reader = av.open(video_path)
    frames = [torch.from_numpy(f.to_rgb().to_ndarray()) for f in reader.decode(video=0)]
    vlen = len(frames)
    duration_sec = get_pyav_video_duration(reader)
    fps = vlen / float(duration_sec)

    if clip is None:
        start_pos, end_pos = 0, 1
    else:
        video_start_sec, video_end_sec = clip
        start_pos, end_pos = video_start_sec / vlen, video_end_sec / vlen
        start_pos, end_pos = max(0, min(start_pos, 1)), max(0, min(end_pos, 1))

    frame_indices = get_frame_indices(
        num_frames, vlen, start_pos, end_pos, sample=sample, fix_start=fix_start,
        input_fps=fps, max_num_frames=max_num_frames
    )
    frames = torch.stack([frames[idx] for idx in frame_indices])  # (T, H, W, C), torch.uint8
    frames = frames.permute(0, 3, 1, 2)  # (T, C, H, W), torch.uint8

    start_frame_index = round(start_pos * vlen)
    frame_indices = [i - start_frame_index for i in frame_indices]
    return frames, frame_indices, fps


def read_frames_gif(video_path, num_frames, sample='rand', fix_start=None, max_num_frames=-1, clip=None, client=None, fps=None):
    gif = imageio.get_reader(video_path)
    vlen = len(gif)
    duration_sec = vlen / fps

    if clip is None:
        start_pos, end_pos = 0, 1
    else:
        video_start_sec, video_end_sec = clip
        start_pos, end_pos = video_start_sec / duration_sec, video_end_sec / duration_sec
        start_pos, end_pos = max(0, min(start_pos, 1)), max(0, min(end_pos, 1))

    frame_indices = get_frame_indices(
        num_frames, vlen, start_pos, end_pos, sample=sample, fix_start=fix_start,
        max_num_frames=max_num_frames
    )
    frames = []
    for index, frame in enumerate(gif):
        # for index in frame_idxs:
        if index in frame_indices:
            frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2RGB)
            frame = torch.from_numpy(frame).byte()
            # # (H x W x C) to (C x H x W)
            frame = frame.permute(2, 0, 1)
            frames.append(frame)
    frames = torch.stack(frames)  # .float() / 255

    start_frame_index = round(start_pos * vlen)
    frame_indices = [i - start_frame_index for i in frame_indices]
    return frames, frame_indices, fps  # for tgif


def read_frames_decord(video_path, num_frames, sample='rand', fix_start=None, max_num_frames=-1, clip=None, client=None, fps=None):
    video_reader = VideoReader(video_path, num_threads=1)
    vlen = len(video_reader)
    fps = video_reader.get_avg_fps()
    duration_sec = vlen / fps

    if clip is None:
        start_pos, end_pos = 0, 1
    else:
        video_start_sec, video_end_sec = clip
        start_pos, end_pos = video_start_sec / duration_sec, video_end_sec / duration_sec
        start_pos, end_pos = max(0, min(start_pos, 1)), max(0, min(end_pos, 1))

    frame_indices = get_frame_indices(
        num_frames, vlen, start_pos, end_pos, sample=sample, fix_start=fix_start,
        input_fps=fps, max_num_frames=max_num_frames
    )
    frames = video_reader.get_batch(frame_indices)  # (T, H, W, C), torch.uint8
    frames = frames.permute(0, 3, 1, 2)  # (T, C, H, W), torch.uint8

    start_frame_index = round(start_pos * vlen)
    frame_indices = [i - start_frame_index for i in frame_indices]
    return frames, frame_indices, float(fps)


def read_frames_images(video_path, num_frames, sample='rand', fix_start=None, max_num_frames=-1, interval_sec=None, clip=None, client=None, fps=3):
    frame_fnames = [os.path.join(video_path, f) for f in sorted(os.listdir(video_path))]
    vlen = len(frame_fnames)
    duration_sec = vlen / fps

    if clip is None:
        start_pos, end_pos = 0, 1
    else:
        video_start_sec, video_end_sec = clip
        start_pos, end_pos = video_start_sec / duration_sec, video_end_sec / duration_sec
        start_pos, end_pos = max(0, min(start_pos, 1)), max(0, min(end_pos, 1))

    frame_indices = get_frame_indices(
        num_frames, vlen, start_pos, end_pos, sample=sample, fix_start=fix_start,
        input_fps=fps, max_num_frames=max_num_frames
    )
    selected_fnames = [frame_fnames[i] for i in frame_indices]
    frames = np.stack([cv2.cvtColor(cv2.imread(fname), cv2.COLOR_BGR2RGB) for fname in selected_fnames])
    # (T x H x W x C) to (T x C x H x W)
    frames = frames.transpose(0, 3, 1, 2)
    frames = torch.from_numpy(frames).to(torch.uint8)

    start_frame_index = round(start_pos * vlen)
    frame_indices = [i - start_frame_index for i in frame_indices]
    return frames, frame_indices, float(fps)


VIDEO_READER_FUNCS = {
    'av': read_frames_av,
    'decord': read_frames_decord,
    'gif': read_frames_gif,
    'frames': read_frames_images,
}