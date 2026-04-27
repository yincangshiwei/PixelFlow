"""
PixelFlow 自动化打包脚本
用法:
    python build.py              # 仅打包 exe（输出到 dist/<APP_NAME>/）
    python build.py --installer  # 打包 exe + 生成 Inno Setup 安装包
    python build.py --clean      # 清理打包产物

所有应用元信息（名称、版本等）统一从 config.py 读取，无需在多处修改。
"""

import subprocess
import shutil
import sys
from pathlib import Path

from config import APP_NAME, APP_VERSION, APP_DESCRIPTION, APP_PUBLISHER

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
SPEC_FILE = ROOT / "PixelFlow.spec"
ISS_TEMPLATE = ROOT / "installer.iss.template"
ISS_FILE = ROOT / "installer.iss"


def clean():
    """清理打包产物"""
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print(f"已清理: {d}")
    if ISS_FILE.exists():
        ISS_FILE.unlink()
        print(f"已清理: {ISS_FILE}")
    print("清理完成")


def generate_iss():
    """从模板生成 installer.iss，替换占位符为 config.py 中的值"""
    if not ISS_TEMPLATE.exists():
        print(f"未找到 Inno Setup 模板: {ISS_TEMPLATE}")
        sys.exit(1)

    content = ISS_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{APP_NAME}": APP_NAME,
        "{APP_VERSION}": APP_VERSION,
        "{APP_DESCRIPTION}": APP_DESCRIPTION,
        "{APP_PUBLISHER}": APP_PUBLISHER,
    }
    for key, value in replacements.items():
        content = content.replace(key, value)

    ISS_FILE.write_text(content, encoding="utf-8")
    print(f"已生成: {ISS_FILE}")


def build_exe():
    """使用 PyInstaller 打包"""
    print("=" * 50)
    print(f"开始 PyInstaller 打包 {APP_NAME} v{APP_VERSION}...")
    print("=" * 50)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC_FILE),
    ]
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("PyInstaller 打包失败!")
        sys.exit(1)

    output_dir = DIST_DIR / APP_NAME
    if output_dir.exists():
        print(f"\n打包成功! 输出目录: {output_dir}")
        total = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
        print(f"总大小: {total / 1024 / 1024:.1f} MB")
    else:
        print("打包产物目录不存在，请检查错误日志")
        sys.exit(1)


def build_installer():
    """使用 Inno Setup 生成安装包"""
    print("\n" + "=" * 50)
    print("开始生成安装包...")
    print("=" * 50)

    # 从模板生成 .iss 文件
    generate_iss()

    # 查找 Inno Setup 编译器（常见安装路径 + PATH 环境变量）
    iscc_paths = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"D:\Inno Setup 6\ISCC.exe",
        r"E:\Inno Setup 6\ISCC.exe",
    ]
    iscc = None
    for p in iscc_paths:
        if Path(p).exists():
            iscc = p
            break

    # 如果固定路径都没找到，尝试从 PATH 中查找
    if iscc is None:
        iscc = shutil.which("ISCC")

    if iscc is None:
        print("未找到 Inno Setup 6 (ISCC.exe)")
        print("请通过以下任一方式解决:")
        print("  1. 将 Inno Setup 安装目录添加到系统 PATH 环境变量")
        print("  2. 设置环境变量: set ISCC=E:\\Inno Setup 6\\ISCC.exe")
        print("  3. 下载安装: https://jrsoftware.org/isdownload.php")
        sys.exit(1)

    print(f"Inno Setup 编译器: {iscc}")

    cmd = [iscc, str(ISS_FILE)]
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("Inno Setup 编译失败!")
        sys.exit(1)

    # 查找生成的安装包
    output_dir = DIST_DIR / "installer"
    installers = list(output_dir.glob("*.exe")) if output_dir.exists() else []
    if installers:
        for f in installers:
            size = f.stat().st_size / 1024 / 1024
            print(f"\n安装包生成成功: {f}")
            print(f"文件大小: {size:.1f} MB")
    else:
        print("安装包输出目录为空，请检查 Inno Setup 日志")


def main():
    args = sys.argv[1:]

    if "--clean" in args:
        clean()
        return

    # 检查 PyInstaller
    try:
        import PyInstaller
        print(f"PyInstaller 版本: {PyInstaller.__version__}")
    except ImportError:
        print("未安装 PyInstaller，正在安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    build_exe()

    if "--installer" in args:
        build_installer()

    print("\n" + "=" * 50)
    print("全部完成!")
    print("=" * 50)


if __name__ == "__main__":
    main()
