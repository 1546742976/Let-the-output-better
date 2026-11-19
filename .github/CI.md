# 持续集成

配置位于 `.github/workflows/ci.yml`。每次 push、Pull Request、手动触发，以及每周一北京时间 09:23 运行。

## 检测范围

| 检测 | 工具 / 判定 |
| --- | --- |
| 工作流与 YAML | actionlint（含 ShellCheck）、yamllint |
| 代码规范与格式 | Ruff lint 与 format --check |
| 类型 | mypy，检查 app，包括未完整标注函数体 |
| 测试与覆盖率 | pytest，行与分支综合覆盖率至少 80% |
| 文档与实跑一致 | README §13 的用例数与综合覆盖率，与本次 `junit.xml`/`coverage.json` 比对（仅 Windows job） |
| 兼容性 | Linux Python 3.11–3.14，Windows Python 3.13 |
| 离线验证 | mock demo、金标评估及 CSV 结果断言 |
| Python 安全 | Bandit |
| 代码扫描 | CodeQL Actions；上传应用代码后追加 Python |
| 依赖漏洞 | pip-audit 检查 CI 工具和应用依赖的解析结果 |
| 密钥泄漏 | Gitleaks，获取完整 Git 历史，各事件按工具规则扫描 |
| 依赖更新 | Dependabot，每周一北京时间 09:00 检查 Python 与 Actions |

测试、覆盖率、安全扫描和离线评估报告作为 Actions artifacts 保存 14 天。
CodeQL 发现展示在仓库 Security → Code scanning；分析执行成功不代表没有安全告警。
`CI result` 汇总失败状态，可作为以后分支保护的 required status check；此配置本身不修改分支保护规则。

## 源码尚未上传时

仓库目前先提交 CI 配置。工作流根据已提交的 `app/*.py` 判断是否存在应用代码。
源码缺失时，应用 lint、类型、测试、离线评估、Bandit 明确显示 skipped，汇总说明“应用检测尚未运行”。
工作流检查、CI 依赖审计、密钥扫描、CodeQL Actions 仍正常执行。
上传 app 后自动启用全部应用检测；缺少 requirements、tests、demo、evaluate 或金标集将报错。

CI 固定 `PROVIDER=mock`，不需要模型 API Key，也不传入模型密钥。
评估脚本输出报告不一定以失败退出，所以额外校验 CSV：样本非空、valid 预期一致、关键字段匹配、事实保持且未降级。
README 中的分数增益、修复率、延迟等研究目标保留在评估报告，不作为这项回归断言的硬阈值。
两类数字区别对待：**机械事实**可由 `.github/scripts/check_readme_metrics.py` 与同一次运行的产物硬比对，漂移即失败；**研究目标**（分数增益、修复率等）依赖数据集与模型配置，仍只呈现于评估报告。
机械事实分两处比对，各自跑在能给出可复现值的那个作业上：

- README §13 的用例数与综合覆盖率——只在 Windows job 比对（§13 记录的是 win32 值，Linux 恒高约 0.15pp，跨平台比对只会产生假告警）。
- README §9 的实测指标表——在离线评估作业比对 `evaluation.metrics.json`。数值由 `scripts/evaluate.py` 自己落成机读汇总，守卫只做比对：在守卫里重算一遍公式就是第二份实现，两份会各自漂移。延迟 P95 一行除外，runner 计时抖动是噪声。

## 本地复现（上传应用代码前可先检查）

```bash
python -m pip install -r requirements.txt -r .github/requirements-ci.txt
python -m ruff check --config .github/ruff.toml app scripts tests .github/scripts
python -m ruff format --check --config .github/ruff.toml app scripts tests .github/scripts
python -m mypy --config-file .github/mypy.ini app
python -m pytest -q --cov=app --cov-branch --cov-fail-under=80
python -m bandit -r app
python -m pip_audit -r requirements.txt
python -m yamllint -c .github/yamllint.yml .github
python scripts/evaluate.py --provider mock --out data/reports/ci.md
python .github/scripts/check_evaluation.py data/reports/ci.csv
python .github/scripts/check_readme_metrics.py --evaluation data/reports/ci.metrics.json
```

所有 Action 固定到完整 commit SHA，Dependabot 负责更新。CI 工具固定版本。
项目依赖沿用 requirements.txt；漏洞扫描针对当次解析的版本，不等同于部署环境的完整锁文件审计。
