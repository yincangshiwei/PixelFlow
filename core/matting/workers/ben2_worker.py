#!/usr/bin/env python3
"""
BEN2 抠图子进程入口 —— 在模型独立 uv 环境中运行。

用法:
  python ben2_worker.py --input in.png --output out.png [--device auto|cpu|cuda]
                        [--refine] --weights-dir DIR [--weights-file FILE]

输出: 成功时 exit 0；失败 exit 1，错误信息写 stderr。
最后一行 stdout 可带 JSON 状态: {"ok": true, "size": [w,h]}
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path


def _load_model(weights_dir: str | None, weights_file: str | None, device: str):
    import torch
    from ben2 import BEN_Base

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    elif device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("请求 CUDA，但当前环境 torch.cuda 不可用")

    model = None
    wdir = Path(weights_dir) if weights_dir else None
    wfile = Path(weights_file) if weights_file else None

    if wdir and (wdir / "model.safetensors").is_file():
        try:
            model = BEN_Base.from_pretrained(str(wdir))
        except Exception:
            model = None

    if model is None and wfile and wfile.is_file():
        suf = wfile.suffix.lower()
        if suf in (".pth", ".pt"):
            model = BEN_Base()
            model.loadcheckpoints(str(wfile))
        elif suf == ".safetensors":
            model = BEN_Base.from_pretrained(str(wfile.parent))

    if model is None and wdir and wdir.is_dir():
        for name in ("BEN2_Base.pth", "pytorch_model.bin", "model.safetensors"):
            p = wdir / name
            if not p.is_file():
                continue
            if p.suffix.lower() in (".pth", ".pt"):
                model = BEN_Base()
                model.loadcheckpoints(str(p))
                break
            try:
                model = BEN_Base.from_pretrained(str(wdir))
                break
            except Exception:
                continue

    if model is None:
        raise RuntimeError(
            "未找到可用权重（需要 model.safetensors 或 BEN2_Base.pth）"
        )

    model.to(device).eval()
    return model, device


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BEN2 matting worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--refine", action="store_true")
    parser.add_argument("--weights-dir", default="")
    parser.add_argument("--weights-file", default="")
    args = parser.parse_args(argv)

    try:
        from PIL import Image

        model, device = _load_model(
            args.weights_dir or None,
            args.weights_file or None,
            args.device,
        )

        img = Image.open(args.input)
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            work = bg
        else:
            work = img.convert("RGB")

        result = model.inference(work, refine_foreground=bool(args.refine))
        if result.mode != "RGBA":
            result = result.convert("RGBA")

        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        result.save(str(out), format="PNG")

        print(json.dumps({"ok": True, "device": device, "size": list(result.size)}))
        return 0
    except Exception as e:
        print(f"BEN2 worker error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
