# 本机文件上传与导入测试

结果文件：[latest.json](latest.json)。测试脚本：[../scripts/benchmark_large_files.py](../scripts/benchmark_large_files.py)。

2026-10-20 在当前 Windows / Python 3.12.4 环境运行，使用独立 Uvicorn 服务、真实本机 HTTP 和 8MiB 分片。每种格式使用全新的服务进程，峰值内存通过 Windows `GetProcessMemoryInfo` 获取，统计整个服务进程（包含原生 DuckDB 内存）。

| 格式 | 文件字节数 | 数据行数 | 上传（秒） | 导入与概览（秒） | 聚合查询（秒） | 服务峰值内存（字节） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CSV | 1,073,741,824 | 2,097,152 | 6.237 | 11.580 | 0.036 | 1,419,943,936 |
| XLSX | 5,633,418 | 100,000 | 0.062 | 10.091 | 0.027 | 146,829,312 |

两种格式均完成行数与销售额求和校验，质量概览状态为 ready；通过删除接口清理测试数据后，再清理独立临时目录。用户已有的数据和账号没有参与测试。脚本不调用模型接口。

CSV 使用 512 字节行及重复的合成内容，压缩特征与真实业务数据可能不同。Excel 是 10 万行文件，不是 1GiB XLSX 测试。磁盘空间记录为导入完成后的持久文件占用，不是整个过程中临时文件占用的峰值。测试结果不能作为任意 1GB 文件的性能保证。

复测会在 `data/` 下创建独立临时目录，并在结束后清理，仅保留结果 JSON：

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_large_files.py --csv-mib 1024 --excel-rows 100000
```
