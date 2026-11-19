# 理赔通信中的事实缺口与核赔依据

本地文本工作区用于保存事故事实和物品实体抽取相关的合成记录，并按设备、事故和费用关系核对核赔材料。

## 环境

需要 Python 3.11 或更新版本。运行程序仅使用 Python 标准库。测试依赖固定为 pytest 8.3.4，可执行 `python -m pip install -r requirements-dev.txt` 安装。

## 核对费用

运行 `PYTHONPATH=src python -m nlp_insurance_evidence_thread.desk_cli` 查看每项费用是否已被设备身份、设备受损和维修范围材料覆盖。照片说明、维修单、报价和上门服务单分开登记。

`--view case` 输出案件级视图，包含逐项状态、缺口、疑问原文、部件归类和仍可独立办理的费用：

```
PYTHONPATH=src python -m nlp_insurance_evidence_thread.desk_cli \
  --case fixtures/case-shared-service.json --view case
```

`--view policy` 只看服务次数、分摊和独立费用。

## 核对口径

* **设备身份**：只有原文写出可核对序列号（例如 `Z91`），或材料登记了目录内设备时，才把材料绑定到该设备。同一句同时出现多台设备、只出现“主机/设备”、或登记对象与原文指向冲突时，不组合使用，并保留产生疑问的原文。
* **部件不是设备**：报案里的“拆下外壳”、登记为 `外壳` 的照片说明都只记为部件，不会成为第二台受损设备。
* **维修范围**：必须由维修动作原文（更换、维修、修理等）或结构化维修单证明。“两台同型主机上门服务一次”只证明一次上门服务，不替任何一台证明维修范围；“更换 Z91 外壳，检查 Z19 但未更换配件”不能把外壳更换复制给 Z19。
* **相反表述**：照片说明写“未见进水痕迹”“外观完好”时，该材料不能作为受损依据，缺口会指向这句原文。
* **共用上门服务**：按服务单号或“同一事故、同一现场、同一上门日期”归并，不能按设备台数放大次数。多项费用引用同一次服务且没有分摊凭据时，判为重复申报；有分摊金额时才分别核赔。
* **原决定**：始终保留。补充材料推翻或收窄服务次数、维修范围时，状态变为“复核原决定”，并列出应复核的具体部分。
* **独立费用**：设备身份、受损、维修范围已足且不含待定共用服务的费用，列入 `independent_costs`，并给出 `basis_by_item` 逐项依据；设备项目已足但共用服务待定的，列入 `device_costs_awaiting_service`。

## 补件请求与回件

补件请求保存在本地 JSON（默认与案件文件同目录，可用 `--store` 指定）：

```
# 针对缺口发出请求；不同缺口要求不同材料
PYTHONPATH=src python -m nlp_insurance_evidence_thread.desk_cli \
  --case fixtures/case-shared-service.json request --cost COST-2

# 登记回件并读回是否填上相应缺口
PYTHONPATH=src python -m nlp_insurance_evidence_thread.desk_cli \
  --case fixtures/case-shared-service.json received --request REQ-1 \
  --case-result fixtures/case-shared-service-updated.json

# 查看仍可独立办理的费用、待复核部分和待补缺口
PYTHONPATH=src python -m nlp_insurance_evidence_thread.desk_cli \
  --case fixtures/case-shared-service.json followup --request REQ-1
```

回件的 `outcome` 会分别列出 `filled`、`still_open` 和 `unclear`；如果新材料证明原本视为两次的服务其实是一次，或只修了其中一台，会给出 `review_parts` 并要求复核对应部分，而不会改动原决定本身。

## 记录工作区

从项目根目录执行 `PYTHONPATH=src python -m nlp_insurance_evidence_thread.cli ingest fixtures/records.json` 导入样例，随后执行 `PYTHONPATH=src python -m nlp_insurance_evidence_thread.cli list` 查看记录。工作数据写入 `var/records.json`。业务查询使用 fixtures/business.json 中的初始资料，约定见 fixtures/business-contract.md。执行 PYTHONPATH=src python -m nlp_insurance_evidence_thread.cli business 可查看当前业务目录结果。

样例为本地合成文本。记录包含来源、修订号和原文，当前工作区对空白做统一处理，并提供字符及英文词项切分。

## 测试

执行 `python -m pytest -q` 检查规范化、记录持久化、来源筛选，以及设备/部件区分、跨设备证据隔离、共用服务次数、原决定复核和补件回读。
