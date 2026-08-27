"""引擎契约(纯数据层):stdlib dataclass + dict→model 构造器。

零 IO 零日志零 envs 的叶子模块;字段名即序化键(wire/落盘 camelCase:
localPath/repoUrl/filePaths/rootSections);wire 契约(出网校验)在
apps/deepwiki-webui/server/schemas.py。选型 dict(choice)= {generator, generator_config},
解析/判等/凭证规则见 utils.py(generator 选型段)。
"""

from dataclasses import dataclass, field


@dataclass
class RepoInfo:
    owner: str
    repo: str
    type: str
    token: str | None = None
    localPath: str | None = None  # 字段名即序化键(localPath/repoUrl is the wire key)
    repoUrl: str | None = None


@dataclass
class WikiPage:
    id: str
    title: str
    content: str
    filePaths: list[str]  # 字段名即序化键(wire/落盘 camelCase)
    importance: str  # 'high' | 'medium' | 'low'
    relatedPages: list[str]


@dataclass
class WikiSection:
    id: str
    title: str
    pages: list[str]
    subsections: list[str] | None = None


@dataclass
class WikiStructureModel:
    id: str
    title: str
    description: str
    pages: list[WikiPage]
    sections: list[WikiSection] | None = None
    rootSections: list[str] | None = None  # 字段名即序化键


def wiki_structure_of(d: dict | None) -> WikiStructureModel | None:
    """dict → WikiStructureModel(嵌套 WikiPage/Section);缺失键按旧契约缺省兜底;None → None。"""
    if d is None:
        return None
    return WikiStructureModel(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        pages=[WikiPage(**p) for p in d.get("pages", [])],
        sections=[
            WikiSection(
                id=s["id"],
                title=s["title"],
                pages=s.get("pages", []),
                subsections=s.get("subsections"),
            )
            for s in d.get("sections") or []
        ],
        rootSections=d.get("rootSections"),
    )


@dataclass
class CodeMapCitation:
    file_path: str  # Repository-relative path of the source file
    start_line: int | None = None  # 1-based line range start
    end_line: int | None = None  # 1-based line range end
    snippet: str = ""  # Verbatim excerpt copied from the source (used to locate the range)


@dataclass
class CodeMapStep:
    id: str  # Human-facing id such as '1a', '1b', '2a'
    label: str  # Short title of the step
    code: str = ""  # Example code snippet illustrating the step
    citation: CodeMapCitation | None = None  # Where this step's code comes from


@dataclass
class CodeMapSection:
    id: str  # Section id such as '1', '2'
    title: str  # Section title
    guide: str = ""  # Prose guide for the section (filled in phase 2)
    diagram: str = ""  # Mermaid diagram source (filled in phase 2)
    steps: list[CodeMapStep] = field(default_factory=list)


@dataclass
class CodeMap:
    title: str  # Overall codemap title
    summary: str = ""  # Introductory summary
    sections: list[CodeMapSection] = field(default_factory=list)


def codemap_of(d: dict) -> CodeMap:
    """dict → CodeMap(递归构造;缺失字段按缺省兜底);坏结构 → ValueError(调用方按失败处理)。

    兜底默认值与旧 pydantic 契约逐字相同(summary/guide/diagram/code = "",steps = [],
    citation = None),保证 codemap 退化路径与 NDJSON 事件形态不漂移。
    """
    try:
        return CodeMap(
            title=d["title"],
            summary=d.get("summary", ""),
            sections=[
                CodeMapSection(
                    id=s["id"],
                    title=s["title"],
                    guide=s.get("guide", ""),
                    diagram=s.get("diagram", ""),
                    steps=[
                        CodeMapStep(
                            id=t["id"],
                            label=t["label"],
                            code=t.get("code", ""),
                            citation=CodeMapCitation(**t["citation"]) if t.get("citation") else None,
                        )
                        for t in s.get("steps", [])
                    ],
                )
                for s in d.get("sections", [])
            ],
        )
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"无效 codemap 数据: {e}") from e
