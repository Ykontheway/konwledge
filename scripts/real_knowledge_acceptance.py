"""真实项目文档的隔离验收: 默认绝对门禁, 不自动认可或更新人工基线。

运行: uv run python scripts/real_knowledge_acceptance.py \
    --output-dir .konwledge/real-knowledge-acceptance

只有最初 quality run 是显式完整评测。后续任务全部由 knowledge import 的
生产事务 hook 触发, 不调用 auto-evaluation run/retry/drain。
query 是独立编写的词面检索需求, 不宣称自然语言或 LLM 语义能力。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

from konwledge.application import KnowledgeLibrary
from konwledge.domain import EvaluationItem, EvaluationSet
from konwledge.domain.evaluation import dump_evaluation_set
from konwledge.quality import QualityPolicy
from konwledge.storage import UnitOfWork, create_session_factory, create_sqlite_engine

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = ("docs/auto-evaluation.md", "docs/acceptance.md")
AUTO_HEADING = "哪些变更会触发"
POSITIVE_OLD = "同一外层事务合并为一个任务"
POSITIVE_NEW = "一个任务合并同一外层事务内的变更"


@dataclass(frozen=True)
class CaseSpec:
    """在任何评测结果出现前固定; 用文档结构而非检索命中确定 gold。"""

    id: str
    query: str
    references: tuple[tuple[str, str], ...] = ()
    no_answer: bool = False


CASES = (
    CaseSpec("idempotent-import", "原样重导", ((SOURCE_PATHS[0], AUTO_HEADING),)),
    CaseSpec(
        "token-budget-parameters",
        "max_tokens token_encoding",
        ((SOURCE_PATHS[1], "Token 预算按真实 tokenizer 计数"),),
    ),
    CaseSpec(
        "shared-quality-thresholds",
        "Recall MRR",
        ((SOURCE_PATHS[0], "先绑定自己的评测集"), (SOURCE_PATHS[1], "默认质量门槛")),
    ),
    CaseSpec("unsupported-backup", "量子备份 恢复时限", no_answer=True),
)


class AcceptanceError(RuntimeError):
    """仅携带可公开的错误码, 不携带 CLI 输出或源文。"""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


@contextmanager
def read_library(database: Path) -> Iterator[KnowledgeLibrary]:
    # SDK 仅用于读取; 建库、评测集导入、配置和所有发布都走正式 CLI。
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        yield KnowledgeLibrary(create_session_factory(engine))
    finally:
        engine.dispose()


def build_dataset(lineages: Mapping[tuple[str, str], UUID]) -> EvaluationSet:
    return EvaluationSet(
        id=uuid4(),
        name="真实项目文档词面检索验收",
        version=1,
        created_at=datetime.now(UTC),
        items=tuple(
            EvaluationItem(
                id=case.id,
                query=case.query,
                expected_lineage_ids=frozenset(lineages[ref] for ref in case.references),
                expect_no_answer=case.no_answer,
            )
            for case in CASES
        ),
    )


def replace_section_body(text: str, heading: str) -> str:
    # 保留原标题和身份, 只移除该真实文档副本的证据, 不代表删除业务功能。
    start = text.index(f"## {heading}\n") + len(f"## {heading}\n")
    end = text.find("\n## ", start)
    if end < 0:
        end = len(text)
    return text[:start] + "\n" + text[end:]


class KnowledgeCounts(TypedDict):
    document_count: int
    unit_count: int
    active_unit_count: int
    heads: list[str]
    revision_ids: list[str]


class Runner:
    def __init__(self, output_dir: Path) -> None:
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = output_dir / (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
        )
        self.run_dir.mkdir(mode=0o700, exist_ok=False)
        self.database = self.run_dir / "knowledge.db"
        self.documents = self.run_dir / "documents"
        self.originals: dict[str, bytes] = {}
        self.commands: list[dict[str, object]] = []
        self.steps: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {}
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "run_id": self.run_dir.name,
            "runner_sha256": sha256(Path(__file__).read_bytes()),
            "retrieval_scope": "lexical-only; 不证明 LLM 语义或生成答案正确性",
            "baseline": {
                "human_accepted": False,
                "bound": False,
                "regression_enabled": False,
                "reason_code": "AWAITING_HUMAN_APPROVAL",
                "reason": (
                    "首次仅验证固定业务集的默认绝对门禁; 初始结果未获人工明确接受,"
                    "不绑定回归 baseline, 不宣称 delta 回归通过。"
                    "人工审核后可另行固定 baseline 与策略;"
                    "本次未将 case.at 冻结到旧知识时间, 未放宽策略。"
                ),
            },
            "sources": [],
            "steps": self.steps,
            "commands": self.commands,
            "checks": self.checks,
            "artifacts": {
                "database": "knowledge.db",
                "evaluation_private": "evaluation.json",
                "originals_private": "original/docs/",
                "mutable_documents_private": "documents/docs/",
                "changed_copies_private": "changes/",
            },
        }

    def check(self, name: str, condition: bool) -> None:
        self.checks[name] = condition
        require(condition, name.upper())

    def cli(self, label: str, *args: str) -> subprocess.CompletedProcess[str]:
        executable = Path(sys.executable).parent / "knowledge"
        require(executable.is_file(), "KNOWLEDGE_CLI_NOT_INSTALLED")
        env = dict(os.environ, NO_COLOR="1", TERM="dumb")
        result = subprocess.run(
            [str(executable), *args, "-d", str(self.database)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        # 不落盘 stdout/stderr 或完整参数: 它们可能包含 query 与绝对路径。
        self.commands.append(
            {
                "label": label,
                "command": "knowledge " + " ".join(args[:2] if args[0] != "import" else args[:1]),
                "exit_code": result.returncode,
                "stdout_hash": sha256(result.stdout.encode()),
                "stderr_hash": sha256(result.stderr.encode()),
                "knowledge_committed_warning": (
                    "knowledge committed; evaluation did not pass" in result.stderr
                ),
            }
        )
        return result

    def cli_json(self, label: str, *args: str) -> Any:
        result = self.cli(label, *args)
        require(result.returncode == 0, "CLI_JSON_EXIT_UNEXPECTED")
        return json.loads(result.stdout)

    def safe(self, value: object) -> None:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
        require(str(ROOT) not in rendered, "REPORT_ABSOLUTE_PATH_LEAK")
        require(str(self.run_dir) not in rendered, "REPORT_RUN_PATH_LEAK")
        require("sqlite+pysqlite:" not in rendered, "REPORT_DATABASE_URL_LEAK")
        for case in CASES:
            require(case.query not in rendered, "REPORT_QUERY_LEAK")
        for raw in self.originals.values():
            text = raw.decode("utf-8")
            require(text not in rendered, "REPORT_SOURCE_LEAK")
            for line in text.splitlines():
                if len(line) > 80:
                    require(line not in rendered, "REPORT_SOURCE_LINE_LEAK")

    def history(self, label: str) -> list[dict[str, Any]]:
        value = self.cli_json(label, "auto-evaluation", "history")
        require(isinstance(value, list), "HISTORY_NOT_JSON_ARRAY")
        self.safe(value)
        return cast(list[dict[str, Any]], value)

    def pin(self) -> dict[str, Any]:
        value = self.cli_json("status", "auto-evaluation", "status")
        require(isinstance(value, dict), "CONFIG_NOT_JSON_OBJECT")
        return cast(dict[str, Any], value)

    def snapshot(self) -> KnowledgeCounts:
        engine = create_sqlite_engine(f"sqlite+pysqlite:///{self.database.as_posix()}")
        try:
            factory = create_session_factory(engine)
            library = KnowledgeLibrary(factory)
            summaries = library.list_documents()
            with UnitOfWork(factory) as uow:
                revision_ids = sorted(str(revision.id) for revision in uow.revisions.list())
            return {
                "document_count": len(summaries),
                "unit_count": sum(len(summary.units) for summary in summaries),
                "active_unit_count": len(library.publishable_units()),
                "heads": sorted(
                    str(summary.document.id) for summary in summaries if summary.is_head
                ),
                "revision_ids": revision_ids,
            }
        finally:
            engine.dispose()

    def prepare(self) -> tuple[EvaluationSet, dict[tuple[str, str], UUID]]:
        git = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        self.report["source_git_commit"] = git.stdout.strip()
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", *SOURCE_PATHS],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        require(tracked.returncode == 0, "SOURCE_NOT_TRACKED")
        if self.run_dir.is_relative_to(ROOT):
            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", str(self.run_dir / "report.json")],
                cwd=ROOT,
                check=False,
            )
            self.check("runtime_artifacts_gitignored", ignored.returncode == 0)
        for relative in SOURCE_PATHS:
            raw = (ROOT / relative).read_bytes()
            self.originals[relative] = raw
            for base in (self.run_dir / "original", self.documents):
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
            self.report["sources"].append({"relative_path": relative, "sha256": sha256(raw)})
        result = self.cli("initial-import", "import", str(self.documents))
        self.check("initial_import_exit_zero", result.returncode == 0)
        self.check("initial_import_no_job", self.history("initial-history") == [])
        references = {ref for case in CASES for ref in case.references}
        lineages: dict[tuple[str, str], UUID] = {}
        with read_library(self.database) as library:
            summaries = library.list_documents()
            self.check("only_real_document_sources", len(summaries) == len(SOURCE_PATHS))
            for relative, heading in sorted(references):
                matches = [
                    unit
                    for summary in summaries
                    if Path(summary.document.locator).resolve() == self.documents / relative
                    for unit in summary.units
                    if unit.heading == heading
                ]
                require(len(matches) == 1, "INITIAL_HEADING_MAPPING_AMBIGUOUS")
                lineages[relative, heading] = matches[0].lineage_id
        dataset = build_dataset(lineages)
        manifest = {
            "case_count": len(CASES),
            "answerable_count": sum(not case.no_answer for case in CASES),
            "multi_evidence_count": sum(len(case.references) > 1 for case in CASES),
            "no_answer_count": sum(case.no_answer for case in CASES),
            "expectations_hash": json_hash(
                [
                    {"id": case.id, "query": case.query, "references": case.references}
                    for case in CASES
                ]
            ),
        }
        self.report["case_manifest"] = manifest
        write_json(self.run_dir / "case-manifest.json", manifest)
        with (self.run_dir / "evaluation.json").open("x", encoding="utf-8") as stream:
            stream.write(dump_evaluation_set(dataset))
        self.report["dataset"] = {
            "id": str(dataset.id),
            "version": dataset.version,
            "hash": dataset.content_hash,
            "case_count": len(dataset.items),
            "file_hash": sha256((self.run_dir / "evaluation.json").read_bytes()),
        }
        result = self.cli(
            "dataset-import", "evaluation", "import", str(self.run_dir / "evaluation.json")
        )
        self.check("dataset_import_exit_zero", result.returncode == 0)
        quality_path = self.run_dir / "initial-quality.json"
        result = self.cli(
            "initial-quality",
            "quality",
            "run",
            "--set-id",
            str(dataset.id),
            "--version",
            "1",
            "--report",
            str(quality_path),
        )
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        self.safe(quality)
        self.report["initial"] = {
            "exit_code": result.returncode,
            "report": quality,
            "job_id": None,
        }
        self.report["initial"]["knowledge"] = self.snapshot()
        self.check("initial_default_quality_passed", result.returncode == 0 and quality["passed"])
        config = self.cli_json(
            "enable", "auto-evaluation", "enable", "--set-id", str(dataset.id), "--version", "1"
        )
        self.check(
            "default_policy_not_lowered",
            config["quality_policy"] == QualityPolicy().model_dump(mode="json"),
        )
        self.check(
            "no_unreviewed_regression_baseline",
            not config["has_baseline"] and not config["has_regression_policy"],
        )
        self.check("enable_creates_no_job", self.history("enabled-history") == [])
        write_json(self.run_dir / "config.json", config)
        self.report["config"] = config
        return dataset, lineages

    def repeat(self, label: str) -> None:
        before_jobs = self.history(label + "-before")
        before_knowledge = self.snapshot()
        result = self.cli(label, "import", str(self.documents))
        after_jobs = self.history(label + "-after")
        after_knowledge = self.snapshot()
        evidence = {
            "exit_code": result.returncode,
            "before_job_count": len(before_jobs),
            "after_job_count": len(after_jobs),
            "before_job_ids": [job["id"] for job in before_jobs],
            "after_job_ids": [job["id"] for job in after_jobs],
            "knowledge_unchanged": before_knowledge == after_knowledge,
            "unchanged_file_count": sum(
                line.startswith("unchanged:") for line in result.stdout.splitlines()
            ),
        }
        self.report[label] = evidence
        self.check(label + "_no_new_job", result.returncode == 0 and before_jobs == after_jobs)
        self.check(label + "_no_new_revision", before_knowledge == after_knowledge)
        self.check(
            label + "_unchanged_files", evidence["unchanged_file_count"] == len(SOURCE_PATHS)
        )

    def change(
        self,
        name: str,
        raw: bytes,
        expected_exit: int,
        status: str,
        dataset: EvaluationSet,
        initial_pin: dict[str, Any],
    ) -> dict[str, Any]:
        before_jobs = self.history(name + "-before")
        before = self.snapshot()
        target = self.documents / SOURCE_PATHS[0]
        target.write_bytes(raw)
        archive = self.run_dir / "changes" / f"{name}.md"
        archive.parent.mkdir(exist_ok=True)
        with archive.open("xb") as stream:
            stream.write(raw)
        result = self.cli(name + "-import", "import", str(target))
        jobs = self.history(name + "-history")
        new = [job for job in jobs if job["id"] not in {old["id"] for old in before_jobs}]
        require(len(new) == 1, "CHANGE_DID_NOT_CREATE_EXACTLY_ONE_JOB")
        job = self.cli_json(name + "-show", "auto-evaluation", "show", new[0]["id"])
        self.safe(job)
        step = {
            "name": name,
            "import_exit_code": result.returncode,
            "copy_hash": sha256(raw),
            "job": job,
            "knowledge": self.snapshot(),
        }
        self.steps.append(step)
        write_json(self.run_dir / f"{name}-job.json", job)
        self.check(name + "_history_show_equal", job == new[0])
        self.check(name + "_exit_expected", result.returncode == expected_exit)
        self.check(name + "_status_expected", job["status"] == status)
        self.check(
            name + "_revision_linked",
            bool(job["trigger_revision_ids"])
            and set(job["trigger_revision_ids"]).issubset(set(step["knowledge"]["revision_ids"]))
            and set(job["trigger_revision_ids"]).isdisjoint(set(before["revision_ids"])),
        )
        self.check(
            name + "_metrics_linked",
            job["report"]["dataset"] == job["dataset"]
            and job["report"]["metrics"]["case_count"] == len(dataset.items),
        )
        self.check(name + "_dataset_hash_frozen", job["dataset"]["hash"] == dataset.content_hash)
        pin = self.pin()
        self.check(name + "_policy_and_pin_frozen", pin == initial_pin)
        with read_library(self.database) as library:
            self.check(
                name + "_dataset_contents_frozen",
                library.evaluation.get(dataset.id, version=1) == dataset,
            )
            heads = [
                summary
                for summary in library.list_documents()
                if summary.is_head and Path(summary.document.locator).resolve() == target
            ]
            self.check(
                name + "_knowledge_committed",
                len(heads) == 1 and heads[0].document.checksum == sha256(raw),
            )
            hits = library.search(CASES[0].query)
            if status == "failed":
                stale_hits = library.search(CASES[0].query, include_stale=True)
                step["removed_evidence_hits"] = len(hits.hits)
                step["include_stale_removed_evidence_hits"] = len(stale_hits.hits)
                self.check("fault_no_old_evidence_fallback", not hits.hits and not stale_hits.hits)
                self.check(
                    "fault_committed_exit_warning",
                    "knowledge committed; evaluation did not pass" in result.stderr,
                )
                self.check("fault_safe_error_code", job["error_code"] == "QUALITY_GATE_FAILED")
            else:
                self.check(
                    name + "_expected_lineage_still_retrievable",
                    any(
                        hit.unit.lineage_id in dataset.items[0].expected_lineage_ids
                        for hit in hits.hits
                    ),
                )
            self.check(name + "_no_stale_automatic_hits", job["report"]["stale_hits"] == 0)
        self.check(name + "_past_jobs_not_overwritten", all(old in jobs for old in before_jobs))
        return cast(dict[str, Any], job)

    def execute(self) -> None:
        dataset, lineages = self.prepare()
        initial_pin = self.pin()
        self.repeat("repeat_initial")
        original = self.originals[SOURCE_PATHS[0]]
        original_text = original.decode("utf-8")
        require(original_text.count(POSITIVE_OLD) == 1, "POSITIVE_CHANGE_ANCHOR_NOT_UNIQUE")
        positive = original_text.replace(POSITIVE_OLD, POSITIVE_NEW).encode("utf-8")
        self.check("positive_copy_really_changed", positive != original)
        self.change("positive", positive, 0, "passed", dataset, initial_pin)
        fault = replace_section_body(positive.decode("utf-8"), AUTO_HEADING).encode("utf-8")
        self.check("fault_copy_really_changed", fault != positive)
        failed_job = self.change("fault", fault, 3, "failed", dataset, initial_pin)
        self.change("restore", original, 0, "passed", dataset, initial_pin)
        self.repeat("repeat_restored")
        final_jobs = self.history("final-history")
        self.check("exactly_three_automatic_jobs", len(final_jobs) == 3)
        self.check("failed_job_preserved_after_restore", failed_job in final_jobs)
        write_json(self.run_dir / "history.json", final_jobs)
        histories: list[dict[str, object]] = []
        with read_library(self.database) as library:
            for (relative, _heading), lineage in sorted(lineages.items()):
                history = library.unit_history(lineage)
                histories.append(
                    {
                        "source_relative_path": relative,
                        "lineage_id": str(history.lineage_id),
                        "version_count": len(history.versions),
                        "versions": [
                            {
                                "unit_id": str(unit.id),
                                "document_id": str(unit.document_id),
                                "content_hash": unit.content_hash,
                                "status": history.statuses[unit.id].value,
                            }
                            for unit in history.versions
                        ],
                        "revision_ids": [str(revision.id) for revision in history.revisions],
                    }
                )
                if lineage in dataset.items[0].expected_lineage_ids:
                    self.check("same_lineage_has_three_versions", len(history.versions) == 3)
                    state = library.unit_state(lineage)
                    self.check(
                        "restore_republishes_original_version",
                        state is not None and state.status.value == "active",
                    )
        self.safe(histories)
        self.report["lineages"] = histories
        write_json(self.run_dir / "lineages.json", histories)
        self.check(
            "evaluation_file_unchanged",
            sha256((self.run_dir / "evaluation.json").read_bytes())
            == self.report["dataset"]["file_hash"],
        )
        self.check(
            "original_sources_unchanged",
            all((ROOT / relative).read_bytes() == raw for relative, raw in self.originals.items()),
        )
        self.check(
            "mutable_copies_restored",
            all(
                (self.documents / relative).read_bytes() == raw
                for relative, raw in self.originals.items()
            ),
        )
        self.report["status"] = "passed"

    def run(self) -> Path:
        try:
            self.execute()
        except Exception as error:
            self.report["status"] = "failed"
            code = (
                str(error) if isinstance(error, AcceptanceError) else type(error).__name__.upper()
            )
            self.report["error_code"] = (
                code if re.fullmatch(r"[A-Z0-9_]+", code) else "RUNNER_ERROR"
            )
        finally:
            self.report["finished_at"] = datetime.now(UTC).isoformat()
            self.safe(self.report)
            write_json(self.run_dir / "report.json", self.report)
        return self.run_dir / "report.json"


def run_acceptance(output_dir: Path) -> Path:
    return Runner(output_dir).run()


def main() -> int:
    parser = argparse.ArgumentParser(description="真实项目文档变更自动评测隔离验收")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".konwledge/real-knowledge-acceptance")
    )
    args = parser.parse_args()
    report_path = run_acceptance(args.output_dir)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "report": str(report_path),
                "error_code": payload.get("error_code"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
