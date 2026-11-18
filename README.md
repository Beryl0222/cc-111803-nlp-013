# 理赔通信中的事实缺口与核赔依据

本地文本工作区用于保存事故事实和物品实体抽取相关的合成记录，并提供基础规范化和词项查看能力。

## 环境

需要 Python 3.11 或更新版本。运行程序仅使用 Python 标准库。测试依赖固定为 pytest 8.3.4，可执行 `python -m pip install -r requirements-dev.txt` 安装。

## 运行

运行 `PYTHONPATH=src python -m nlp_insurance_evidence_thread.desk_cli` 查看每项费用是否已被设备受损与维修范围材料覆盖。照片说明与维修单分开登记。已出具的决定作为原处置记录回查。

从项目根目录执行 `PYTHONPATH=src python -m nlp_insurance_evidence_thread.cli ingest fixtures/records.json` 导入样例，随后执行 `PYTHONPATH=src python -m nlp_insurance_evidence_thread.cli list` 查看记录。工作数据写入 `var/records.json`。业务查询使用 fixtures/business.json 中的初始资料，约定见 fixtures/business-contract.md。执行 PYTHONPATH=src python -m nlp_insurance_evidence_thread.cli business 可查看当前业务目录结果。

样例为本地合成文本。记录包含来源、修订号和原文，当前工作区对空白做统一处理，并提供字符及英文词项切分。

## 测试

执行 `python -m pytest -q` 检查规范化、记录持久化和来源筛选。
