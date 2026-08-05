#!/usr/bin/env python3
"""
BEN2 抠图子进程入口 —— 在模型独立 uv 环境中运行。

用法（单次）:
  python ben2_worker.py --input in.png --output out.png [--device auto|cpu|cuda]
                        [--refine] --weights-dir DIR [--weights-file FILE]

用法（常驻，批量复用模型）:
  python ben2_worker.py --serve [--device auto|cpu|cuda]
                        --weights-dir DIR [--weights-file FILE]
  启动后先输出一行 JSON: {"ok":true,"event":"ready","device":"..."}
  随后从 stdin 逐行读 JSON 请求，每请求回一行 JSON：
    {"cmd":"ping"}
    {"cmd":"infer","input":"...","output":"...","refine":false}
    {"cmd":"shutdown"}

输出约定: 业务状态只写 stdout（一行一个 JSON）；诊断信息可写 stderr。
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


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _prepare_rgb(img):
    from PIL import Image

    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _infer_one(model, input_path: str, output_path: str, refine: bool) -> dict:
    from PIL import Image

    img = Image.open(input_path)
    work = _prepare_rgb(img)
    result = model.inference(work, refine_foreground=bool(refine))
    if result.mode != "RGBA":
        result = result.convert("RGBA")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.save(str(out), format="PNG")
    return {"ok": True, "size": list(result.size)}


def _run_oneshot(args) -> int:
    try:
        model, device = _load_model(
            args.weights_dir or None,
            args.weights_file or None,
            args.device,
        )
        meta = _infer_one(model, args.input, args.output, bool(args.refine))
        meta["device"] = device
        _emit(meta)
        return 0
    except Exception as e:
        print(f"BEN2 worker error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "error": str(e)})
        return 1


def _run_serve(args) -> int:
    try:
        model, device = _load_model(
            args.weights_dir or None,
            args.weights_file or None,
            args.device,
        )
    except Exception as e:
        print(f"BEN2 worker error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "event": "ready", "error": str(e)})
        return 1

    _emit({"ok": True, "event": "ready", "device": device})

    for raw in sys.stdin:
        line = (raw or "").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _emit({"ok": False, "error": f"无效 JSON 请求: {e}"})
            continue

        if not isinstance(req, dict):
            _emit({"ok": False, "error": "请求必须是 JSON 对象"})
            continue

        cmd = str(req.get("cmd") or "").lower()
        if cmd in ("shutdown", "exit", "quit"):
            _emit({"ok": True, "event": "bye"})
            return 0
        if cmd == "ping":
            _emit({"ok": True, "event": "pong", "device": device})
            continue
        if cmd != "infer":
            _emit({"ok": False, "error": f"未知命令: {cmd or '(空)'}"})
            continue

        inp = req.get("input") or ""
        outp = req.get("output") or ""
        refine = bool(req.get("refine", False))
        if not inp or not outp:
            _emit({"ok": False, "error": "infer 需要 input 与 output 路径"})
            continue
        try:
            meta = _infer_one(model, str(inp), str(outp), refine)
            meta["device"] = device
            _emit(meta)
        except Exception as e:
            print(f"BEN2 worker infer error: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            _emit({"ok": False, "error": str(e)})

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BEN2 matting worker")
    parser.add_argument("--serve", action="store_true", help="常驻模式：stdin JSON 协议")
    parser.add_argument("--input", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--refine", action="store_true")
    parser.add_argument("--weights-dir", default="")
    parser.add_argument("--weights-file", default="")
    args = parser.parse_args(argv)

    if args.serve:
        return _run_serve(args)

    if not args.input or not args.output:
        parser.error("单次模式需要 --input 与 --output；批量请使用 --serve")
    return _run_oneshot(args)


if __name__ == "__main__":
    raise SystemExit(main())
