"""Shared report styles and validated, versioned task snapshots."""
import json
from threading import RLock
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator

from . import config
from .storage import atomic_json

REPORT_MODES = frozenset({'text', 'illustrated'})
DEFAULT_STYLE = {
    'id': '0' * 32,
    'revision': 1,
    'name': '视频详细流程（默认）',
    'built_in': True,
    'prompt': '沿视频顺序按主题划分章节，详细整理技术结论、机制、规格、演示和已知限制。'
              '正文以原文为依据，开头提炼核心结论。图文版在对应段落插入通过复核的配图。',
}
LOCK = RLock()
Rule = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1400)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60, pattern=r'^[^\r\n]+$')]
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
StyleId = Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{32}$')]


class Section(BaseModel):
    title: Name
    focus: Rule = ""


class StyleRules(BaseModel):
    order: Literal["timeline", "topic"]
    sections: list[Section] = Field(default_factory=list, max_length=20)
    evidence: Rule = ""
    writing: Rule = ""
    introduction_title: Name = "前文"
    introduction: Rule = ""
    audit: Rule = ""
    visuals: Rule = ""
    annotate_figures: bool = False
    missing: Literal["omit", "label"] = "omit"

    @model_validator(mode='after')
    def validate_sections(self):
        titles = [section.title.casefold() for section in self.sections]
        if self.order == "topic" and (not titles or len(set(titles)) != len(titles)):
            raise ValueError("按主题编排时需要提供不重复的栏目")
        if self.order == "timeline" and titles:
            raise ValueError("按时间编排时请将取材偏好写入取材规则")
        return self


class StyleDraft(BaseModel):
    name: Name
    prompt: Prompt


class StyleSave(StyleDraft):
    rules: StyleRules
    revision: int | None = Field(default=None, ge=1)


class StyleSelection(BaseModel):
    id: StyleId
    revision: int = Field(ge=1)


class StyleConflict(ValueError):
    pass


def _load():
    try:
        return json.loads((config.DATA / 'report-styles.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def list_styles():
    with LOCK:
        styles = _load()
        default = styles.pop(DEFAULT_STYLE['id'], DEFAULT_STYLE.copy())
        custom = sorted(styles.values(), key=lambda item: (item['name'].casefold(), item['id']))
    return [default, *custom]


def save_style(style_id, body):
    default = style_id == DEFAULT_STYLE['id']
    with LOCK:
        styles = _load()
        if style_id is None:
            if len(styles) - int(DEFAULT_STYLE['id'] in styles) >= 100:
                raise ValueError("报告风格已达 100 个")
            style_id = uuid4().hex
            revision = 1
        else:
            current = styles.get(style_id, DEFAULT_STYLE if default else None)
            if current is None or body.revision != current['revision']:
                raise StyleConflict("报告风格已更新，请重新加载后编辑")
            revision = current['revision'] + 1
        name = body.name.casefold()
        if not default and (name == DEFAULT_STYLE['name'].casefold() or any(
                item['name'].casefold() == name and item['id'] != style_id for item in styles.values())):
            raise ValueError("已存在同名报告风格")
        style = dict(body.model_dump(exclude={'revision'}), id=style_id, revision=revision)
        if default:
            style.update(name=DEFAULT_STYLE['name'], built_in=True)
        styles[style_id] = style
        atomic_json(config.DATA / 'report-styles.json', styles, private=True)
        return style


def task_options(options, modes):
    values = options.model_dump(exclude={'report_style'})
    selection = options.report_style
    if not REPORT_MODES.intersection(modes):
        if selection is not None:
            raise ValueError("请选择图文汇报或文字汇报后再使用报告风格")
        return values
    style_id = selection.id if selection else DEFAULT_STYLE['id']
    with LOCK:
        style = _load().get(style_id)
    if style is None and style_id == DEFAULT_STYLE['id']:
        style = DEFAULT_STYLE.copy()
    if selection is not None and (style is None or style['revision'] != selection.revision):
        raise StyleConflict("报告风格已更新，请重新选择")
    values['report_style'] = style
    return values


def parse_style(engine, body):
    prompt = """将用户的一份报告要求整理成可执行的设置。保留明确的栏目原名、原顺序、前文位置、业务术语和条件分支。
如果用户指定按主题归类或规定栏目顺序，order 为 topic，sections 列出正文栏目，每项含 title 和 focus；前文独立放 introduction，并将标题放 introduction_title，默认前文。
如果只指定语气、篇幅、段落布局或取材重点，order 为 timeline，sections 返回空数组。missing 为 label 仅当要求保留空栏目，否则为 omit。
evidence 描述取材重点，writing 描述正文写法与段落布局，introduction 描述前文格式，audit 描述条件与核对规则，visuals 描述配图偏好。
用户明确要求裁切标注、编号红框或部件旁注时，annotate_figures 为 true，否则为 false。
缺省字段返回空字符串。保留条件和资料不足时的处理。示例车型、参数、评价只是写法示例，不能当作事实。
仅返回 JSON 对象，包含 order、sections、evidence、writing、introduction_title、introduction、audit、visuals、annotate_figures、missing。
用户的报告要求：
""" + body.prompt

    def valid(value):
        try:
            StyleRules.model_validate(value)
        except ValidationError:
            return False
        return True

    result = engine.report_json("解析报告风格", prompt, valid,
                                system="你将报告要求转换为结构化设置。只输出 JSON。", max_tokens=3000)
    return StyleRules.model_validate(result)
