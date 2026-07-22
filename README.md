# 脑部 MRI 开放文献采集器

本目录提供一个面向 RAG 的**合规开放文献**采集最小实现。它只调用公开 API：

- PubMed E-utilities：检索和下载文献元数据（指南、综述、系统综述、典型临床研究、图书类条目）；
- Europe PMC：检索带开放获取标记的文献，并下载可用的 JATS XML 全文；
- 本地 XML：按章节和段落转换为带出处的 RAG chunks。

采集范围按导师要求覆盖脑部 MRI 相关**著作 / 期刊论文 / 指南**，疾病方向包括：

| 大类 | 典型主题 |
|------|----------|
| 脑血管类疾病 | 缺血/出血性卒中、动脉瘤、烟雾病等 |
| 脑肿瘤性疾病 | 胶质瘤、脑膜瘤、脑转移瘤等 |
| 神经内科疾病 | 多发性硬化、癫痫、帕金森、阿尔茨海默等 |
| 精神类疾病 | 精神分裂症、抑郁、双相、自闭症等 |

质量优先策略：优先 **Guideline / Practice Guideline / Consensus**、**Systematic Review / Meta-Analysis / Review**，并限制近年英文文献；每条 query 默认最多 40 条，避免低质量灌水。

它不会绕过登录、付费墙、验证码、robots 限制或访问医院系统；CNKI、万方、维普等付费库应通过机构授权的数据交付或官方导出功能接入。

## 快速开始

需要 Python 3.10+，不依赖第三方包。

```powershell
conda activate mri_rag
cd "c:\Users\muli\Documents\脑部MRI大模型领域知识注入"

# 建议先清空旧 data，再按新 query 重采
python collector.py --config collection_config.json
python build_rag_chunks.py --input data\europepmc_fulltext --metadata data\europepmc_metadata.jsonl --output data\rag_chunks.jsonl
```

运行前确认 `collection_config.json` 中已填写联系邮箱；如有 NCBI API Key，可填写 `ncbi_api_key`。可先把 `max_records_per_query` 调到 `10` 做小规模试跑。

## 输出

```text
data/
  pubmed_metadata.jsonl          # PubMed 元数据与摘要（含疾病大类、文档类型标签）
  europepmc_metadata.jsonl       # Europe PMC 元数据与开放获取标记
  europepmc_fulltext/            # 仅下载成功的开放全文 XML
  collection_manifest.jsonl      # 每次下载的审计记录
  rag_chunks.jsonl               # 可进入检索系统的知识片段
```

每条记录会保留 PMID/PMCID/DOI、来源 URL、`disease_categories`、`doc_types`、查询标签与抓取时间。全量入库前仍应由项目法务/数据治理人员核验每篇全文的实际许可。
