from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from corporate_kb.domscribe_agent import DomscribeGigaCodeAgent
from corporate_kb.gigacode_runner import GigaCodeJsonResult


class FakeRunner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "available": True, "error": None}

    def run_workspace_edit(self, *, prompt: str, **_kwargs: Any) -> GigaCodeJsonResult:
        self.prompts.append(prompt)
        return GigaCodeJsonResult(
            payload={
                "status": "completed",
                "message": "Готово",
                "changed_files": ["apps/dashboard/src/App.tsx"],
                "verification": "Исходник проверен",
            },
            analyzed_files=("apps/dashboard/src/App.tsx",),
            session_id="test",
            model="fake",
            duration_ms=1,
            usage={},
        )


class FakeRelay:
    def __init__(self, annotations: list[dict[str, Any]]) -> None:
        self.annotations = deque(annotations)
        self.responses: list[tuple[str, str]] = []
        self.status_updates: list[tuple[str, str, str | None]] = []

    def claim_next(self) -> dict[str, Any] | None:
        return self.annotations.popleft() if self.annotations else None

    def respond(self, annotation_id: str, message: str) -> dict[str, Any]:
        self.responses.append((annotation_id, message))
        return {}

    def update_status(
        self,
        annotation_id: str,
        status: str,
        *,
        error_details: str | None = None,
    ) -> dict[str, Any]:
        self.status_updates.append((annotation_id, status, error_details))
        return {}

    def status(self) -> dict[str, Any]:
        return {
            "relay": {"port": 4318},
            "annotations": {
                "queued": len(self.annotations),
                "processing": 0,
                "processed": len(self.status_updates),
                "failed": 0,
                "archived": 0,
            },
        }


def _annotation(annotation_id: str, intent: str, source: Path) -> dict[str, Any]:
    return {
        "found": True,
        "annotationId": annotation_id,
        "userIntent": intent,
        "element": {"tagName": "button", "innerText": "Run"},
        "sourceLocation": {
            "file": str(source),
            "line": 10,
            "column": 2,
            "componentName": "App",
            "tagName": "button",
        },
        "runtimeContext": {"componentProps": {"disabled": False}},
    }


def test_domscribe_agent_processes_annotations_in_fifo_order(
    settings_factory,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "apps" / "dashboard" / "src" / "App.tsx"
    source.parent.mkdir(parents=True)
    source.write_text("export function App() { return null }\n", encoding="utf-8")
    relay = FakeRelay(
        [
            _annotation("ann_ABCDEFGH_1000", "Сделай кнопку синей", source),
            _annotation("ann_ABCDEFGH_2000", "Добавь иконку", source),
        ]
    )
    runner = FakeRunner()
    agent = DomscribeGigaCodeAgent(
        settings_factory(domscribe_workspace_root=workspace),
        runner=runner,  # type: ignore[arg-type]
        relay=relay,  # type: ignore[arg-type]
    )

    assert agent.process_once() is True
    assert agent.process_once() is True
    assert agent.process_once() is False

    assert "Сделай кнопку синей" in runner.prompts[0]
    assert "Добавь иконку" in runner.prompts[1]
    assert "untrusted application data" in runner.prompts[0]
    assert [item[:2] for item in relay.status_updates] == [
        ("ann_ABCDEFGH_1000", "processed"),
        ("ann_ABCDEFGH_2000", "processed"),
    ]
    assert all("Файлы: apps/dashboard/src/App.tsx" in message for _, message in relay.responses)
