#!/usr/bin/env python3
"""
RMBG 2.0 抠图子进程入口 —— 在模型独立 uv 环境中运行。

基于 BRIA AI RMBG-2.0（BiRefNet + transformers AutoModelForImageSegmentation）。
协议与 ben2_worker 对齐，便于主进程统一调度。

用法（单次）:
  python rmbg2_worker.py --input in.png --output out.png [--device auto|cpu|cuda]
                        [--return-mask] --weights-dir DIR [--weights-file FILE]

用法（常驻，批量复用模型）:
  python rmbg2_worker.py --serve [--device auto|cpu|cuda]
                        --weights-dir DIR [--weights-file FILE]
  启动后先输出一行 JSON:
    {"ok":true,"event":"ready","device":"...","vram_gb":8.0,"recommend_batch":2}
  随后从 stdin 逐行读 JSON 请求，每请求回一行 JSON：
    {"cmd":"ping"}
    {"cmd":"infer","input":"...","output":"...","refine":false,"return_mask":true}
    {"cmd":"infer_batch","return_mask":true,"items":[{"input":"...","output":"..."}, ...]}
    {"cmd":"shutdown"}

输出约定:
  - 业务状态只写 stdout（一行一个 JSON）
  - 诊断信息可写 stderr
  - return_mask=true 时 output 为单通道 L 遮罩 PNG（尺寸=输入图）
  - return_mask=false 时 output 为 RGBA 前景 PNG
  - infer_batch 仅支持 return_mask（真 tensor batch）
  - refine 参数保留协议兼容，RMBG2 无独立精炼，忽略该标志
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

# 与官方 README / BEN2 业务侧预缩放一致
_INFER_SIZE = (1024, 1024)
_IMAGENET_MEAN = [0.485, 0.456, 0.406]
_IMAGENET_STD = [0.229, 0.224, 0.225]


def _load_model(weights_dir: str | None, weights_file: str | None, device: str):
    import torch
    from transformers import AutoModelForImageSegmentation

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    elif device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("请求 CUDA，但当前环境 torch.cuda 不可用")

    candidates: list[Path] = []
    wdir = Path(weights_dir) if weights_dir else None
    wfile = Path(weights_file) if weights_file else None

    if wdir and wdir.is_dir():
        candidates.append(wdir)
    if wfile and wfile.is_file():
        candidates.append(wfile.parent)

    model = None
    last_err: Exception | None = None
    for path in candidates:
        # 目录内需有 config + 权重，供 trust_remote_code 本地加载
        if not (path / "config.json").is_file() and not any(path.glob("*.safetensors")):
            if not any(path.glob("*.bin")):
                continue
        try:
            model = AutoModelForImageSegmentation.from_pretrained(
                str(path),
                trust_remote_code=True,
                local_files_only=True,
            )
            break
        except Exception as e:
            last_err = e
            model = None
            try:
                # 本地标记不完整时再试（允许补全缓存，一般仍用本地文件）
                model = AutoModelForImageSegmentation.from_pretrained(
                    str(path),
                    trust_remote_code=True,
                )
                break
            except Exception as e2:
                last_err = e2
                model = None

    if model is None:
        detail = f"（{last_err}）" if last_err else ""
        raise RuntimeError(
            "未找到可用 RMBG-2.0 权重目录（需要 model.safetensors + config.json 等）"
            + detail
        )

    model.to(device).eval()
    try:
        if device == "cuda":
            torch.backends.cudnn.benchmark = True
    except Exception:
        pass
    return model, device


def _build_transform():
    from torchvision import transforms

    return transforms.Compose(
        [
            transforms.Resize(_INFER_SIZE),
            transforms.ToTensor(),
            transforms.Normalize(_IMAGENET_MEAN, _IMAGENET_STD),
        ]
    )


def _device_meta(device: str) -> dict:
    """ready 时附带显存与建议 batch，供主进程自适应。"""
    meta = {"device": device, "vram_gb": 0.0, "recommend_batch": 1}
    if device != "cuda":
        return meta
    try:
        import torch

        if not torch.cuda.is_available():
            return meta
        props = torch.cuda.get_device_properties(0)
        vram = float(props.total_memory) / (1024 ** 3)
        meta["vram_gb"] = round(vram, 2)
        # RMBG2 ~BiRefNet 比 BEN2 更吃显存，略保守
        if vram < 6.0:
            meta["recommend_batch"] = 1
        elif vram < 10.0:
            meta["recommend_batch"] = 2
        else:
            meta["recommend_batch"] = 3
    except Exception:
        pass
    return meta


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _prepare_rgb(img):
    from PIL import Image

    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode == "LA":
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def _is_oom(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    keys = (
        "out of memory",
        "cuda out of memory",
        "oom",
        "cublas_status_alloc_failed",
        "cudnn_status_alloc_failed",
        "hip out of memory",
    )
    return any(k in msg for k in keys)


def _predict_masks(model, device: str, pil_rgbs: list, transform) -> list:
    """
    对若干 RGB PIL 图推理，返回与输入等长的 L 模式 mask（尺寸=各图原始 size）。
    """
    import torch
    from PIL import Image
    from torchvision import transforms as T

    if not pil_rgbs:
        return []

    tensors = [transform(im) for im in pil_rgbs]
    batch = torch.stack(tensors, dim=0).to(device)

    try:
        with torch.inference_mode():
            preds = model(batch)[-1].sigmoid().detach().cpu()
    except Exception as e:
        if _is_oom(e):
            try:
                if device == "cuda":
                    torch.cuda.empty_cache()
            except Exception:
                pass
            raise RuntimeError(
                f"CUDA OOM during forward (batch={len(pil_rgbs)}): {e}"
            ) from e
        raise

    # preds: (B, 1, H, W) 或 (B, H, W)
    if preds.dim() == 3:
        preds = preds.unsqueeze(1)

    to_pil = T.ToPILImage()
    masks = []
    for i, rgb in enumerate(pil_rgbs):
        matte = preds[i].squeeze().clamp(0.0, 1.0)
        mask = to_pil(matte)
        if mask.mode != "L":
            mask = mask.convert("L")
        if mask.size != rgb.size:
            mask = mask.resize(rgb.size, resample=Image.BILINEAR)
        masks.append(mask)
    return masks


def _infer_one(
    model,
    device: str,
    transform,
    input_path: str,
    output_path: str,
    refine: bool,
    *,
    return_mask: bool = False,
) -> dict:
    from PIL import Image

    # refine 保留协议位，RMBG2 无独立精炼实现
    _ = refine

    img = Image.open(input_path)
    work = _prepare_rgb(img)
    mask = _predict_masks(model, device, [work], transform)[0]

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if return_mask:
        mask.save(str(out), format="PNG")
        return {
            "ok": True,
            "size": list(mask.size),
            "output_kind": "mask",
        }

    rgba = work.convert("RGBA")
    rgba.putalpha(mask)
    rgba.save(str(out), format="PNG")
    return {
        "ok": True,
        "size": list(rgba.size),
        "output_kind": "rgba",
    }


def _infer_batch_masks(model, device: str, transform, items: list[dict]) -> dict:
    """
    真 tensor batch：stack → 一次 forward → 逐张写 mask。
    items: [{"input": path, "output": path}, ...]
    输入图建议已是 ≤1024（主进程阶段1预缩放）；transform 内仍会 Resize 到 1024。
    """
    from PIL import Image

    if not items:
        raise ValueError("infer_batch items 为空")

    pil_rgbs = []
    out_paths = []
    for it in items:
        inp = it.get("input") or ""
        outp = it.get("output") or ""
        if not inp or not outp:
            raise ValueError("infer_batch 每项需要 input 与 output")
        img = Image.open(str(inp))
        work = _prepare_rgb(img)
        pil_rgbs.append(work)
        out_paths.append(Path(str(outp)))

    masks = _predict_masks(model, device, pil_rgbs, transform)

    results_meta = []
    for mask, out in zip(masks, out_paths):
        out.parent.mkdir(parents=True, exist_ok=True)
        mask.save(str(out), format="PNG")
        w, h = mask.size
        results_meta.append(
            {"ok": True, "size": [w, h], "output_kind": "mask"}
        )

    return {
        "ok": True,
        "output_kind": "mask",
        "batch_size": len(items),
        "items": results_meta,
    }


def _run_oneshot(args) -> int:
    try:
        model, device = _load_model(
            args.weights_dir or None,
            args.weights_file or None,
            args.device,
        )
        transform = _build_transform()
        meta = _infer_one(
            model,
            device,
            transform,
            args.input,
            args.output,
            bool(args.refine),
            return_mask=bool(args.return_mask),
        )
        meta.update(_device_meta(device))
        _emit(meta)
        return 0
    except Exception as e:
        print(f"RMBG2 worker error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        err = {"ok": False, "error": str(e)}
        if _is_oom(e):
            err["oom"] = True
        _emit(err)
        return 1


def _run_serve(args) -> int:
    try:
        model, device = _load_model(
            args.weights_dir or None,
            args.weights_file or None,
            args.device,
        )
        transform = _build_transform()
    except Exception as e:
        print(f"RMBG2 worker error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "event": "ready", "error": str(e)})
        return 1

    ready = {"ok": True, "event": "ready"}
    ready.update(_device_meta(device))
    _emit(ready)

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
            pong = {"ok": True, "event": "pong"}
            pong.update(_device_meta(device))
            _emit(pong)
            continue

        if cmd == "infer_batch":
            items = req.get("items") or []
            refine = bool(req.get("refine", False))
            return_mask = bool(req.get("return_mask", True))
            if refine:
                # 与 BEN2 一致：batch 路径不跑 refine（本模型本身无 refine）
                _emit({
                    "ok": False,
                    "error": "infer_batch 不支持 refine，请逐张 infer",
                    "oom": False,
                })
                continue
            if not return_mask:
                _emit({
                    "ok": False,
                    "error": "infer_batch 仅支持 return_mask=true",
                })
                continue
            if not isinstance(items, list) or not items:
                _emit({"ok": False, "error": "infer_batch 需要非空 items"})
                continue
            try:
                meta = _infer_batch_masks(model, device, transform, items)
                meta["device"] = device
                _emit(meta)
            except Exception as e:
                print(f"RMBG2 worker infer_batch error: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                err = {"ok": False, "error": str(e), "oom": _is_oom(e)}
                _emit(err)
            continue

        if cmd != "infer":
            _emit({"ok": False, "error": f"未知命令: {cmd or '(空)'}"})
            continue

        inp = req.get("input") or ""
        outp = req.get("output") or ""
        refine = bool(req.get("refine", False))
        return_mask = bool(req.get("return_mask", False))
        if not inp or not outp:
            _emit({"ok": False, "error": "infer 需要 input 与 output 路径"})
            continue
        try:
            meta = _infer_one(
                model,
                device,
                transform,
                str(inp),
                str(outp),
                refine,
                return_mask=return_mask,
            )
            meta["device"] = device
            _emit(meta)
        except Exception as e:
            print(f"RMBG2 worker infer error: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            err = {"ok": False, "error": str(e), "oom": _is_oom(e)}
            _emit(err)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RMBG-2.0 matting worker")
    parser.add_argument("--serve", action="store_true", help="常驻模式：stdin JSON 协议")
    parser.add_argument("--input", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument(
        "--refine",
        action="store_true",
        help="协议兼容位；RMBG2 无独立边缘精炼，忽略",
    )
    parser.add_argument(
        "--return-mask",
        action="store_true",
        help="仅输出单通道 mask PNG",
    )
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
