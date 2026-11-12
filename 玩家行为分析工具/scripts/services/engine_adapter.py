"""Late-bound adapter for the analysis engine.

The HTTP layer intentionally knows nothing about pandas or workbook details.
This adapter accepts a small functional contract while remaining tolerant of
older, semantically named entry points during local migration.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence

from .source_service import json_safe


class EngineUnavailableError(RuntimeError):
    pass


class AnalysisEngineAdapter:
    PLAYER_PREFLIGHT = (
        "preflight_player",
        "preflight_player_data",
        "validate_player_input",
    )
    ONLINE_PREFLIGHT = (
        "preflight_online",
        "preflight_online_data",
        "validate_online_input",
    )
    PLAYER_RUN = (
        "run_player_analysis",
        "analyze_player_data",
        "run_stage_one",
    )
    ONLINE_RUN = (
        "run_online_analysis",
        "analyze_online_data",
        "run_stage_two",
    )

    def __init__(self, module_name: str = "analysis_engine", module: ModuleType | None = None):
        self.module_name = module_name
        self._module = module
        self._target: Any | None = None

    def describe(self) -> dict[str, Any]:
        try:
            target = self._load_target()
        except Exception as exc:  # bootstrap must remain available when engine import fails
            return {
                "available": False,
                "module": self.module_name,
                "message": f"分析引擎未就绪：{exc}",
            }
        return {
            "available": any(self._find(target, names) for names in (self.PLAYER_RUN, self.ONLINE_RUN)),
            "module": self.module_name,
            "version": str(getattr(self._module, "ENGINE_VERSION", getattr(target, "version", "unknown"))),
            "player_ready": self._find(target, self.PLAYER_RUN) is not None,
            "online_ready": self._find(target, self.ONLINE_RUN) is not None,
        }

    def preflight_player(self, **kwargs: Any) -> dict[str, Any]:
        return self._optional_call(self.PLAYER_PREFLIGHT, kwargs)

    def preflight_online(self, **kwargs: Any) -> dict[str, Any]:
        return self._optional_call(self.ONLINE_PREFLIGHT, kwargs)

    def run_player(self, **kwargs: Any) -> dict[str, Any]:
        return self._required_call(self.PLAYER_RUN, kwargs, "玩家行为分析")

    def run_online(self, **kwargs: Any) -> dict[str, Any]:
        return self._required_call(self.ONLINE_RUN, kwargs, "三维分类")

    def _load_target(self) -> Any:
        if self._target is not None:
            return self._target
        if self._module is None:
            self._module = importlib.import_module(self.module_name)
        engine_class = getattr(self._module, "AnalysisEngine", None)
        self._target = engine_class() if inspect.isclass(engine_class) else self._module
        return self._target

    @staticmethod
    def _find(target: Any, names: Sequence[str]) -> Callable[..., Any] | None:
        for name in names:
            value = getattr(target, name, None)
            if callable(value):
                return value
        return None

    def _optional_call(self, names: Sequence[str], kwargs: Mapping[str, Any]) -> dict[str, Any]:
        try:
            target = self._load_target()
        except ModuleNotFoundError:
            return {}
        function = self._find(target, names)
        if function is None:
            return {}
        result = self._invoke(function, kwargs)
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise TypeError("预检接口必须返回 dict")
        return dict(json_safe(result))

    def _required_call(
        self, names: Sequence[str], kwargs: Mapping[str, Any], label: str
    ) -> dict[str, Any]:
        try:
            target = self._load_target()
        except Exception as exc:
            raise EngineUnavailableError(f"{label}引擎无法加载：{exc}") from exc
        function = self._find(target, names)
        if function is None:
            raise EngineUnavailableError(f"{label}引擎未提供可调用入口")
        result = self._invoke(function, kwargs)
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise TypeError(f"{label}引擎必须返回 dict")
        return dict(json_safe(result))

    @staticmethod
    def _invoke(function: Callable[..., Any], values: Mapping[str, Any]) -> Any:
        signature = inspect.signature(function)
        accepts_kwargs = any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in signature.parameters.values()
        )
        if accepts_kwargs:
            return function(**dict(values))
        aliases: dict[str, Any] = {
            **values,
            "sources": values.get("source_files"),
            "files": values.get("source_files"),
            "input_files": values.get("source_files"),
            "draft_dir": values.get("work_dir"),
            "job_dir": values.get("work_dir"),
            "progress": values.get("progress_callback"),
            "update_progress": values.get("progress_callback"),
            "log": values.get("log_callback"),
            "logger": values.get("log_callback"),
            "player_result": values.get("player_job"),
            "base_job": values.get("player_job"),
        }
        call_kwargs: dict[str, Any] = {}
        missing: list[str] = []
        for name, parameter in signature.parameters.items():
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL):
                if parameter.kind is inspect.Parameter.POSITIONAL_ONLY and parameter.default is inspect.Parameter.empty:
                    missing.append(name)
                continue
            if name in aliases:
                call_kwargs[name] = aliases[name]
            elif parameter.default is inspect.Parameter.empty:
                missing.append(name)
        if missing:
            raise TypeError(f"分析引擎入口缺少适配参数：{', '.join(missing)}")
        return function(**call_kwargs)
