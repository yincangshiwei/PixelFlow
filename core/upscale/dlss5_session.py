"""DLSS5 原生 worker 的 stdin/stdout 二进制协议实现。

DLSS5 便携包的原生渲染入口是 ``bin/runtime/host/nvngx.dll``（以 DLL 命名的
可执行文件，ReShade/RenoDX 的 "signed snippet caller check" 要求宿主映像名为
nvngx.dll）。它在 ``host/`` 目录下启动后由 ``dxgi.dll``(ReShade) 加载
``renodx-dlss5.addon64``，再拉起 ``nvngx_dlssnr.dll`` 的 **feature-18**
（DLSS 5 Neural Rendering）。

本模块只用标准库 ``struct`` + ``subprocess`` 复刻该协议，因此：
- 主程序与打包 exe **零新增依赖**（不需要 numpy / opencv / gradio / ffmpeg）
- 不需要为 DLSS5 单独创建 Python 环境

协议（小端，与上游 version-4 model-preset protocol 对齐）::

    会话头  <14I4f  VIDEO_MAGIC, in_w, in_h, out_w, out_h, warmup, frame_count,
                    perf_quality, dlss_model_preset, profile, preset, style,
                    auto_mask, ui_correction,
                    intensity, local_tone, local_structure, skin_structure
    setup   <12I    SETUP_MAGIC, ok, ngx_result, render_w, render_h,
                    out_w, out_h, min_w, min_h, max_w, max_h, applied_model_preset
    帧头    <4Iq    FRAME_MAGIC, index, reset, 0, pts
    帧数据          RGBA uint8 (render_w*render_h*4) + motion float16 全零 (render_w*render_h*4)
    帧回    <5Iq    OUT_MAGIC, index, ok, byte_count, ngx_result, pts
    帧输出          RGBA uint8 (out_w*out_h*4)

静态图片没有运动矢量：上游同样送全零 motion + ``reset=True``，此处一致。
"""
from __future__ import annotations

import struct
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── 协议常量 ──
VIDEO_MAGIC = 0x34563544
SETUP_MAGIC = 0x34505553
FRAME_MAGIC = 0x314D5246
OUT_MAGIC = 0x3154554F

VIDEO_HEADER_FORMAT = "<14I4f"
SETUP_RESPONSE_FORMAT = "<12I"
FRAME_HEADER_FORMAT = "<4Iq"
OUT_HEADER_FORMAT = "<5Iq"

VIDEO_HEADER_SIZE = struct.calcsize(VIDEO_HEADER_FORMAT)
SETUP_RESPONSE_SIZE = struct.calcsize(SETUP_RESPONSE_FORMAT)
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER_FORMAT)
OUT_HEADER_SIZE = struct.calcsize(OUT_HEADER_FORMAT)

# feature-18 真实生效的三条 ReShade 日志证据（缺一不可）
FEATURE_18_EVIDENCE: tuple[tuple[str, str], ...] = (
    ("runtime", "signed DLSSNR 310.8.0 D3D12 runtime initialized"),
    ("created", "feature 18 created via the signed snippet"),
    ("evaluated", "inline feature 18 evaluation succeeded"),
)

# stderr 环形缓冲上限
_STDERR_TAIL = 500


class DLSS5Error(RuntimeError):
    """DLSS5 渲染相关错误。"""

    def __init__(self, message: str = "", *, cancelled: bool = False):
        super().__init__(message)
        self.cancelled = bool(cancelled)


class DLSS5Cancelled(DLSS5Error):
    """用户取消。"""

    def __init__(self, message: str = "已取消 DLSS5 渲染"):
        super().__init__(message, cancelled=True)


@dataclass(frozen=True)
class NativeSettings:
    """传给原生 worker 的神经渲染参数（已完成范围校验）。"""
    perf_quality: int = 0            # DLSS 模式：0=Performance 1=Balanced 2=Quality 3=Ultra 5=DLAA
    dlss_model_preset: int = 0       # 0=Default 10=J 11=K 12=L 13=M
    profile: int = 0
    preset: int = 0                  # NR Preset
    style: int = 0                   # NR Style
    auto_mask: int = 0
    ui_correction: int = 0
    intensity: float = 1.0           # NR Intensity
    local_tone: float = 1.0
    local_structure: float = 1.0
    skin_structure: float = -1.0
    warmup_frames: int = 0


@dataclass
class SessionInfo:
    """setup 协商结果。"""
    render_width: int = 0
    render_height: int = 0
    output_width: int = 0
    output_height: int = 0
    min_width: int = 0
    min_height: int = 0
    max_width: int = 0
    max_height: int = 0
    setup_result: int = 0
    applied_model_preset: int = 0
    pid: int = 0


def _read_exact(stream, size: int) -> bytes:
    """从二进制流精确读取 size 字节；不足则 EOFError。"""
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError(f"原生 worker 在输出 {len(chunks)}/{size} 字节后停止")
        chunks.extend(block)
    return bytes(chunks)


def _read_exact_into(stream, buf: bytearray, size: int) -> None:
    view = memoryview(buf)
    offset = 0
    while offset < size:
        count = stream.readinto(view[offset:size])
        if not count:
            raise EOFError(f"原生 worker 在输出 {offset}/{size} 字节后停止")
        offset += count


def _drain_stderr(stream, sink: list[str], lock: threading.Lock) -> None:
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            with lock:
                sink.append(line)
                if len(sink) > _STDERR_TAIL:
                    del sink[0: len(sink) - _STDERR_TAIL]
    except Exception:
        pass


class DLSS5Session:
    """一次 DLSS5 渲染会话（= 一个原生 worker 进程）。

    同一会话内所有帧必须共享输入/输出尺寸；尺寸变化需重建会话。
    上游按输出尺寸对图片分组复用会话，本类提供同样能力。
    """

    def __init__(
        self,
        *,
        worker_path: Path,
        host_dir: Path,
        reshade_log: Optional[Path],
        input_width: int,
        input_height: int,
        output_width: int,
        output_height: int,
        frame_count: int,
        settings: NativeSettings,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.worker_path = Path(worker_path)
        self.host_dir = Path(host_dir)
        self.reshade_log = Path(reshade_log) if reshade_log else None
        self.input_width = int(input_width)
        self.input_height = int(input_height)
        self.output_width = int(output_width)
        self.output_height = int(output_height)
        self.frame_count = max(1, int(frame_count))
        self.settings = settings
        self.cancel = cancel_event if cancel_event is not None else threading.Event()

        self._proc: Optional[subprocess.Popen] = None
        self._stderr_sink: list[str] = []
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._log_base_size = 0
        self._log_base_tail = b""
        self._sent = 0
        self.closed = False
        self.info = SessionInfo()

    # ── 生命周期 ──

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def render_width(self) -> int:
        return self.info.render_width or self.input_width

    @property
    def render_height(self) -> int:
        return self.info.render_height or self.input_height

    def worker_logs(self) -> list[str]:
        with self._stderr_lock:
            return list(self._stderr_sink)

    def start(self) -> SessionInfo:
        """启动 worker 并完成 setup 协商。"""
        if self._proc is not None:
            return self.info
        if not self.worker_path.is_file():
            raise DLSS5Error(f"找不到 DLSS5 渲染入口: {self.worker_path}")

        self._snapshot_reshade_log()

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                # 上游对图片与视频共用 --video 模式（图片按单帧视频送入，reset=True）
                [str(self.worker_path), "--video"],
                cwd=str(self.host_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
            )
        except OSError as e:
            raise DLSS5Error(
                f"启动 DLSS5 渲染入口失败: {e}\n"
                f"路径: {self.worker_path}\n"
                "该文件虽以 .dll 命名，但实际是可执行程序；"
                "若被安全软件拦截或 quarantined，请加入白名单后重试。"
            ) from e

        self.info.pid = int(self._proc.pid or 0)
        self._stderr_thread = threading.Thread(
            target=_drain_stderr,
            args=(self._proc.stderr, self._stderr_sink, self._stderr_lock),
            name="dlss5-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        s = self.settings
        header = struct.pack(
            VIDEO_HEADER_FORMAT,
            VIDEO_MAGIC,
            self.input_width, self.input_height,
            self.output_width, self.output_height,
            int(s.warmup_frames), self.frame_count,
            int(s.perf_quality), int(s.dlss_model_preset),
            int(s.profile), int(s.preset), int(s.style),
            int(s.auto_mask), int(s.ui_correction),
            float(s.intensity), float(s.local_tone),
            float(s.local_structure), float(s.skin_structure),
        )
        try:
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._proc.stdin.write(header)
            self._proc.stdin.flush()
            try:
                data = _read_exact(self._proc.stdout, SETUP_RESPONSE_SIZE)
            except EOFError as exc:
                code = self._safe_wait(10)
                raise DLSS5Error(
                    "DLSS5 渲染入口在 setup 阶段即退出 "
                    f"(exit={code})，协议不兼容或初始化失败。\n"
                    f"{self._diagnosis_tail()}"
                ) from exc
        except DLSS5Error:
            self.abort()
            raise
        except Exception as exc:
            self.abort()
            raise DLSS5Error(f"DLSS5 setup 写入失败: {exc}") from exc

        (
            magic, ok, setup_result, render_w, render_h,
            nego_w, nego_h, min_w, min_h, max_w, max_h, applied_preset,
        ) = struct.unpack(SETUP_RESPONSE_FORMAT, data)

        if magic != SETUP_MAGIC:
            self.abort()
            raise DLSS5Error(
                "DLSS5 渲染入口返回了未知的 setup 协议标识 "
                f"(0x{magic:08X}，期望 0x{SETUP_MAGIC:08X})。\n"
                "本实现对应 version-4 model-preset 协议；若运行时包版本过旧，"
                "请到「配置 → 高清放大引擎」重新安装后再试。"
            )
        if not ok:
            self.abort()
            raise DLSS5Error(
                f"DLSS 无法在 {self.output_width}×{self.output_height} 下初始化 "
                f"(NGX 0x{setup_result:08X})。请降低放大档位或更新 NVIDIA 驱动。\n"
                f"{self._diagnosis_tail()}"
            )
        if (nego_w, nego_h) != (self.output_width, self.output_height):
            self.abort()
            raise DLSS5Error(
                f"DLSS5 返回的输出尺寸 {nego_w}×{nego_h} 与请求的 "
                f"{self.output_width}×{self.output_height} 不一致"
            )
        if int(applied_preset) != int(s.dlss_model_preset):
            self.abort()
            raise DLSS5Error(
                f"DLSS5 应用的模型预设为 {applied_preset}，"
                f"与请求的 {int(s.dlss_model_preset)} 不一致"
            )
        if render_w < 64 or render_h < 64:
            self.abort()
            raise DLSS5Error(
                f"DLSS 返回的渲染尺寸 {render_w}×{render_h} 不受支持，"
                "宽高都必须至少 64 像素"
            )

        self.info = SessionInfo(
            render_width=int(render_w), render_height=int(render_h),
            output_width=int(nego_w), output_height=int(nego_h),
            min_width=int(min_w), min_height=int(min_h),
            max_width=int(max_w), max_height=int(max_h),
            setup_result=int(setup_result),
            applied_model_preset=int(applied_preset),
            pid=self.info.pid,
        )
        return self.info

    def process_frame(
        self,
        *,
        index: int,
        rgba: bytes,
        reset: bool = True,
        pts: int = 0,
    ) -> bytes:
        """送入一帧 RGBA（尺寸须为 render_width×render_height），返回输出 RGBA 字节。"""
        if self.cancel.is_set():
            raise DLSS5Cancelled()
        if self._proc is None:
            raise DLSS5Error("DLSS5 会话未启动")

        rw, rh = self.render_width, self.render_height
        expected_in = rw * rh * 4
        if len(rgba) != expected_in:
            raise DLSS5Error(
                f"帧数据长度 {len(rgba)} 与渲染尺寸 {rw}×{rh} 所需的 {expected_in} 不符"
            )

        # 静态图片无运动矢量：全零 float16（与上游一致）
        motion = bytes(rw * rh * 4)
        out_size = self.info.output_width * self.info.output_height * 4

        assert self._proc.stdin is not None and self._proc.stdout is not None
        try:
            self._proc.stdin.write(
                struct.pack(FRAME_HEADER_FORMAT, FRAME_MAGIC, int(index), int(bool(reset)), 0, int(pts))
            )
            self._proc.stdin.write(rgba)
            self._proc.stdin.write(motion)
            self._proc.stdin.flush()
        except Exception as exc:
            raise DLSS5Error(f"向 DLSS5 渲染入口写入第 {index} 帧失败: {exc}") from exc

        try:
            head = _read_exact(self._proc.stdout, OUT_HEADER_SIZE)
        except EOFError as exc:
            code = self._safe_wait(10)
            raise DLSS5Error(self.classify_failure(code, index)) from exc

        magic, out_index, ok, byte_count, ngx_result, _out_pts = struct.unpack(
            OUT_HEADER_FORMAT, head
        )
        if magic != OUT_MAGIC or not ok or int(out_index) != int(index) or int(byte_count) != out_size:
            self.abort()
            raise DLSS5Error(
                f"DLSS5 第 {index} 帧响应无效 "
                f"(magic=0x{magic:08X}, ok={ok}, index={out_index}, bytes={byte_count}/{out_size})"
            )
        if int(ngx_result) != 1:
            self.abort()
            raise DLSS5Error(
                f"DLSS 5 feature-18 在第 {index} 帧求值失败 (NGX 0x{ngx_result:08X})\n"
                f"{self._diagnosis_tail()}"
            )

        buf = bytearray(out_size)
        try:
            _read_exact_into(self._proc.stdout, buf, out_size)
        except EOFError as exc:
            code = self._safe_wait(10)
            raise DLSS5Error(self.classify_failure(code, index)) from exc
        self._sent += 1
        return bytes(buf)

    # ── 关闭 ──

    def close(self) -> Optional[int]:
        """关闭会话，返回 worker 退出码（不抛错）。

        - 已按声明的 ``frame_count`` 送满帧 → 优雅关闭（关 stdin 等 worker 自行退出）
        - 只送了部分帧（会话池复用 / 提前结束）→ 直接终止，与上游取消路径一致，
          避免 worker 因等待剩余帧而挂住
        """
        if self.closed and self._proc is None:
            return None
        proc = self._proc
        self.closed = True
        self._proc = None
        if proc is None:
            return None

        graceful = self._sent >= self.frame_count
        code: Optional[int] = None
        try:
            if graceful:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
                try:
                    code = proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    graceful = False
        except Exception:
            graceful = False

        if not graceful:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        code = proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        code = proc.poll()
                else:
                    code = proc.poll()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        self._join_stderr(2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        return code

    def abort(self) -> None:
        """强制终止（取消 / 出错时）。"""
        if self.closed and self._proc is None:
            return
        proc = self._proc
        self.closed = True
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._join_stderr(2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

    def __enter__(self) -> "DLSS5Session":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            try:
                self.close()
            except Exception:
                self.abort()
        else:
            self.abort()

    # ── 诊断 ──

    def _safe_wait(self, timeout: float) -> Optional[int]:
        proc = self._proc
        if proc is None:
            return None
        try:
            return proc.wait(timeout=timeout)
        except Exception:
            return proc.poll()

    def _join_stderr(self, timeout: float) -> None:
        t = self._stderr_thread
        if t is not None:
            try:
                t.join(timeout=timeout)
            except Exception:
                pass
            self._stderr_thread = None

    def _snapshot_reshade_log(self) -> None:
        p = self.reshade_log
        if p is None or not p.is_file():
            self._log_base_size = 0
            self._log_base_tail = b""
            return
        try:
            size = p.stat().st_size
            self._log_base_size = size
            tail = min(256, size)
            with p.open("rb") as f:
                f.seek(size - tail)
                self._log_base_tail = f.read(tail)
        except OSError:
            self._log_base_size = 0
            self._log_base_tail = b""

    def reshade_log_text(self) -> str:
        """只返回本会话期间新增的 ReShade 日志（优先按 [pid] 过滤）。"""
        p = self.reshade_log
        if p is None or not p.is_file():
            return ""
        try:
            with p.open("rb") as f:
                size = p.stat().st_size
                base = self._log_base_size
                tail = self._log_base_tail
                can_seek = size >= base
                if can_seek and tail:
                    f.seek(base - len(tail))
                    can_seek = f.read(len(tail)) == tail
                f.seek(base if can_seek else 0)
                data = f.read()
        except OSError:
            return ""
        text = data.decode("utf-8", errors="replace")
        marker = f"[{self.info.pid}]"
        lines = [ln for ln in text.splitlines() if marker in ln]
        return "\n".join(lines) if lines else text

    def reshade_evidence(self, limit: int = 300) -> list[str]:
        lines = self.reshade_log_text().splitlines()
        relevant = [
            ln for ln in lines
            if "DLSS 5 Neural Rendering" in ln or "DLSSNR" in ln
            or "feature 18" in ln or "exception" in ln.lower()
            or "failed" in ln.lower()
        ]
        return (relevant or lines)[-limit:]

    def verify_feature_18(self) -> dict[str, object]:
        """校验 DLSS 5 feature-18 是否**真的**执行过。

        没有这一步，渲染链路失败时会静默退化成普通缩放而用户毫无感知。
        """
        log = self.reshade_log_text()
        hits = {key: (needle in log) for key, needle in FEATURE_18_EVIDENCE}
        if not all(hits.values()):
            missing = [
                needle for (key, needle), ok in zip(FEATURE_18_EVIDENCE, hits.values()) if not ok
            ]
            evidence = [
                ln for ln in log.splitlines()
                if "DLSS 5 Neural Rendering" in ln or "DLSSNR" in ln or "feature 18" in ln
            ]
            raise DLSS5Error(
                "渲染流程跑完了，但未验证到 DLSS 5 feature-18 真实执行。\n"
                "缺失证据:\n  " + "\n  ".join(missing)
                + ("\nReShade 日志片段:\n" + "\n".join(evidence[-30:]) if evidence else
                   "\nReShade 未产生任何 DLSSNR 相关日志。")
            )
        return {
            "verified": True,
            "evidence": {key: ok for key, ok in hits.items()},
            "nr_upscaling_active": ("[upscaling]" in log),
            "nr_native_fallback": ("NR upscaling fell back to native" in log),
        }

    def classify_failure(self, worker_code: Optional[int], frame_index: int) -> str:
        """把原生/add-on 失败翻译成可操作的诊断文本。"""
        logs = self.worker_logs()
        reshade = self.reshade_evidence()
        evidence = "\n".join([*logs, *reshade])
        access_violation = (
            "evaluate raised 0xC0000005" in evidence
            or "feature 18 evaluate raised an exception" in evidence
        )
        if access_violation:
            summary = (
                f"DLSS 5 feature-18 在第 {frame_index} 帧完成前触发访问违例 0xC0000005，"
                "崩溃发生在神经运行时/add-on 求值内部（D3D12/NGX 初始化本身可能已成功）。"
                "请更新 NVIDIA 驱动，或换用上游已验证的运行时组合。"
            )
        else:
            summary = f"DLSS5 渲染入口在第 {frame_index} 帧完成前退出 (exit={worker_code})。"
        details = [summary, self._diagnosis_tail()]
        return "\n".join(d for d in details if d)

    def _diagnosis_tail(self) -> str:
        bits: list[str] = []
        logs = self.worker_logs()
        if logs:
            bits.append("worker 输出:\n" + "\n".join(logs[-40:]))
        reshade = self.reshade_evidence(60)
        if reshade:
            bits.append("ReShade feature-18 日志:\n" + "\n".join(reshade))
        return "\n".join(bits)


__all__ = [
    "VIDEO_MAGIC", "SETUP_MAGIC", "FRAME_MAGIC", "OUT_MAGIC",
    "VIDEO_HEADER_FORMAT", "SETUP_RESPONSE_FORMAT",
    "FRAME_HEADER_FORMAT", "OUT_HEADER_FORMAT",
    "VIDEO_HEADER_SIZE", "SETUP_RESPONSE_SIZE",
    "FRAME_HEADER_SIZE", "OUT_HEADER_SIZE",
    "FEATURE_18_EVIDENCE",
    "DLSS5Error", "DLSS5Cancelled",
    "NativeSettings", "SessionInfo", "DLSS5Session",
]
