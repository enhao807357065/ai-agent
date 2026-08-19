import subprocess

result = subprocess.run(
    ["pwd"],                # 支持列表和字符串，字符串可能会有注入风险
    capture_output=True,    # 捕获stdout和stderr，否则直接终端打印
    text=True,              # 是否文本输出，不设置默认为bytes
    check=True              # 失败时自动抛异常，returncode!=0的时候会抛CalledProcessError
)
print(result.returncode)    # 返回码
print(result.stdout)        # 命令的标准输出
print(result.stderr)        # 错误输出

result1 = subprocess.run(
    ["python", "-c", "import sys; print(sys.stdin.read().upper())"],
    input="hello test",
    capture_output=True,
    text=True,
    check=True
)
print(f"result1: {result1}")

# 检查命令是否安装
result2 = subprocess.run(
    ["which", "ffmpeg"],
    capture_output=True,
    text=True,
    check=True
)

if result2.returncode == 0:
    print(f"ffmpeg已安装")

# 带超时保护
# try:
#     subprocess.run(
#         ["python", "sandbox_runner.py"],
#         timeout=60,
#         capture_output=True,
#         text=True,
#         check=True,
#     )
# except subprocess.TimeoutExpired as e:
#     print(f"超时了：{e}")
# except subprocess.CalledProcessError as e:
#     print(f"执行失败：{e}")

import asyncio
import sys
import tempfile
import time
from pathlib import Path

async def run_python_in_sandbox(code: str, timeout_sec: int = 3) -> dict:
    """
    在临时目录中执行一段python代码，并返回最小输出结果
    """
    def _run() -> dict:
        start = time.perf_counter()
        with tempfile.TemporaryDirectory() as tempdir:
            script_path = Path(tempdir) / "main.py"
            script_path.write_text(code, encoding="utf-8")

            try:
                proc = subprocess.run(
                    [sys.executable, str(script_path)],
                    cwd=tempdir,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    env={"PYTHONIOENCODING": "utf-8"}
                )
                duration_ms = int((time.perf_counter()-start)*1000)
                stderr = proc.stderr.strip()
                if proc.returncode == 0:
                    error_type = None
                    error_message = ""
                elif "SyntaxError" in stderr:
                    error_type = "syntax_error"
                    error_message = "Python 代码存在语法错误"
                else:
                    error_type = "runtime_error"
                    error_message = "Python 代码运行时抛出异常"

                return {
                    "ok": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "error_type": error_type,
                    "error_message": error_message,
                    "duration_ms": duration_ms,
                    "sandbox": {
                        "cwd": tempdir,
                        "timeout_sec": timeout_sec
                    }
                }
            except subprocess.TimeoutExpired as e:
                print(f"命令执行超时：{e}")
                duration_ms = int((time.perf_counter()-start)*1000)
                return {
                    "ok": False,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": f"Execution timed out after {timeout_sec}s",
                    "error_type": "timeout",
                    "error_message": f"Python 代码执行超时，超过 {timeout_sec} 秒仍未返回",
                    "duration_ms": duration_ms,
                    "sandbox": {
                        "cwd": tempdir,
                        "timeout_sec": timeout_sec
                    }
                }

    subprocess_result = await asyncio.to_thread(_run)
    print(f"subprocess_result: {subprocess_result}")
    return subprocess_result

if __name__ == '__main__':
    print("test")
    print(f"file: {__file__}")
    # 如果在这里进行tab，执行临时文件会报缩进问题！
    code_str = """
import time
print(f"当前时间是：{time.time()}")
    """
    asyncio.run(run_python_in_sandbox(code_str))