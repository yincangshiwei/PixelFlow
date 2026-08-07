"""WorkerFactory —— 按 RunRequest 分派 ProcessWorker / FileProcessWorker。

编排器不直接 new Worker；本工厂是 Worker 创建的唯一入口，
便于测试注入替身、并在 P6 统一校验打包收集。

Worker 参数口径与既有 _begin_process 完全一致：
- output_dir     已解析的输出根目录（原图模式=首文件所在目录）
- auto_subfolder OutputPolicy.effective_auto_subfolder()
- overwrite      仅「原图路径(覆盖原图)」为 True
- file_overwrite 桌面/自定义「覆盖同名文件」；原图覆盖恒 True；副本恒 False
- rel_path_map   保留目录结构时的相对路径映射
- file_index_map 续跑/重试保持原批次 1-based 序号
"""
from __future__ import annotations

from core.worker import ProcessWorker
from core.file_worker import FileProcessWorker
from services.contracts.run_request import RunRequest


def create_worker_for_request(
    request: RunRequest,
    processor,
    *,
    output_dir: str,
    rel_path_map: dict,
    file_index_map: dict,
):
    """按 RunRequest.kind 创建对应 Worker（未启动）。"""
    policy = request.output
    file_list = request.file_list()
    if request.kind == "image":
        return ProcessWorker(
            file_list, output_dir, processor, request.options,
            auto_subfolder=policy.effective_auto_subfolder(),
            overwrite=policy.src_overwrite,
            file_overwrite=policy.resolve_file_overwrite(),
            rel_path_map=rel_path_map,
            file_index_map=file_index_map,
        )
    return FileProcessWorker(
        file_list, output_dir, processor, request.options,
        auto_subfolder=policy.effective_auto_subfolder(),
        rel_path_map=rel_path_map,
        file_index_map=file_index_map,
    )
