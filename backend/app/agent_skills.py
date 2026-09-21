"""Load project-owned agent instructions; record hashes for auditability."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "skills"


def skill_text(role: str, source_type: str | None = None) -> str:
    raw = (ROOT / f"case-{role}" / "SKILL.md").read_text(encoding="utf-8")
    body = raw.split("---", 2)[2].strip()
    if role in {"score", "review"}:
        rubric = (ROOT / "case-score" / "references" / "rubric.md").read_text(encoding="utf-8")
        body += "\n\n# 运行时加载的评分规则\n" + rubric
    if source_type == 'douyin' and role in {"scout", "extract", "verify", "score", "review"}:
        body += "\n\n# 抖音来源的附加证据规则（其他来源不适用）\n" + skill_text('douyin')
    return body


def skill_versions(source_type: str | None = None) -> dict[str, str]:
    return {role: hashlib.sha256(skill_text(role, source_type).encode()).hexdigest()[:12]
            for role in ("scout", "extract", "verify", "score", "review", "douyin")}
