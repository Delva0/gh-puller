"""Expose composable searches and original evidence from frozen offline GitHub facts.

FTS5 ranks text; the source-backed change index connects exact Git paths to PR
comparisons. Every returned relation is checked against source bytes and native
Git. Scores are retrieval signals, never evidence of a fix. Latest incomplete
observations stay incomplete; nothing falls back to an older complete record.
"""

import asyncio
import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

from playground.graphub import change_index
from playground.graphub.audit import select
from playground.graphub.gates import pointer
from playground.graphub.stack_search import payload

from .agent import Tool


class GitHub:
    def __init__(self, source: Path, text: Path, changes: Path, git: Path, scope: dict):
        """Bind derived indexes to their immutable source boundary.

        Args:
            source: Canonical GitHub SQLite archive, opened read-only.
            text: Frozen FTS5 corpus without benchmark-thread exclusions.
            changes: Sealed native PR comparison index.
            git: Canonical local Git store; no fetch or repository execution occurs.
            scope: Repository, cutoff and selected_digest of the captured facts.
        """
        self.paths = source, text, changes
        self.git = git
        self.scope = {key: scope[key] for key in ("repository", "cutoff", "selected_digest")}
        self.resources = ExitStack()
        self.lock = asyncio.Lock()

    def _open(self):
        self.db, self.text, self.changes = [self.resources.enter_context(closing(sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False,
        ))) for path in self.paths]
        if (self.db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone()
                != (self.scope["repository"],)
                or select(self.db, self.scope["cutoff"]) != self.scope["selected_digest"]):
            raise ValueError("GitHub source differs from the frozen evidence boundary")
        # Only derived indexes hold a read transaction; canonical ingestion must keep checkpointing.
        self.text.execute("BEGIN")
        self.changes.execute("BEGIN")
        metadata = {key: json.loads(value) for key, value in self.text.execute("SELECT key,value FROM meta")}
        if metadata["schema"] != 1 or metadata["scope"] != self.scope or metadata["excluded"]:
            raise ValueError("Text index differs from the unexcluded frozen corpus")
        if self.text.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Text index integrity check failed")
        self.change_meta = change_index.verify_index(self.changes, self.scope)
        self.coverage = self.db.execute(
            "SELECT family,coverage,COUNT(*) FROM selected GROUP BY family,coverage",
        ).fetchall()

    async def _work(self, function, *arguments):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(function, *arguments))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # SQLite and native Git must finish before their owner closes resources.
                try:
                    await task
                finally:
                    raise exc

    async def __aenter__(self):
        try:
            await self._work(self._open)
        except BaseException:  # Failed preparation also owns any already-open connections.
            self.resources.close()
            raise
        return self

    async def __aexit__(self, *_exc):
        self.resources.close()

    def _source(self, number, family):
        row = self.db.execute(
            "SELECT o.id,o.coverage,o.payload_digest,p.payload FROM selected o "
            "LEFT JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.family=? AND o.resource_number=?",
            (family, number),
        ).fetchone()
        if row is None:
            return {"number": number, "family": family, "coverage": "not_observed"}, None
        identity = {"number": number, "family": family, "observation": row[0], "coverage": row[1], "digest": row[2]}
        return identity, payload(row[2], row[3]) if row[3] is not None else None

    def _thread(self, number):
        _, document = self._source(number, "issue")
        value = (document.get("value") or {}) if document else {}
        return {"title": value.get("title"), "url": value.get("html_url")}

    def search(self, query, limit=10):
        """Return source-verified text hits from distinct threads.

        Args:
            query: Native FTS5 expression, without inferred terms or filters.
            limit: Maximum distinct threads; each is represented by its best matching document.
        """
        if limit < 1:
            raise ValueError("Search limit must be positive")
        found = {}
        for number, oid, digest, location, text, score, snippet in self.text.execute(
            "SELECT number,observation,digest,location,text,rank,snippet(docs,4,'[',']','...',32) "
            "FROM docs WHERE docs MATCH ? ORDER BY rank,rowid", (query,),
        ):
            if number in found:
                continue
            row = self.db.execute("SELECT family FROM selected WHERE id=?", (oid,)).fetchone()
            if row is None:
                raise ValueError("Text hit is not a frozen latest observation")
            source, document = self._source(number, row[0])
            if (source.get("observation"), source.get("digest"), source["coverage"]) != (oid, digest, "complete"):
                raise ValueError("Text hit differs from its canonical observation")
            value = pointer(document, location)
            expected = ((value.get("title") or "") + "\n" + (value.get("body") or "")
                        if isinstance(value, dict) else "\n" + value)
            if text != expected:
                raise ValueError("Indexed text differs from original evidence")
            found[number] = source | self._thread(number) | {"pointer": location, "score": score, "snippet": snippet}
            if len(found) == limit:
                break
        return {"scope": self.scope, "matches": list(found.values()),
                "limitation": "Only complete observed text families are indexed; no match does not prove absence."}

    def changed(self, paths, kind, limit=10):
        """Return source- and Git-verified changed-path matches.

        Args:
            paths: Exact repository-relative UTF-8 Git paths, without prefix or basename guessing.
            kind: Proposal or landing comparison, as defined by change_index.
            limit: Maximum distinct PRs, represented by their best matching comparison.
        """
        hits = change_index.search(self.changes, [path.encode() for path in paths], kind=kind,
                                   method="overlap", limit=limit)
        verified = change_index.verify_hits(self.db, self.git, self.changes, hits, self.scope)
        output = []
        for hit in hits:
            sources, detail = self.changes.execute("SELECT sources,detail FROM changes WHERE id=?",
                                                   (hit["change"],)).fetchone()
            output.append(hit | self._thread(hit["number"]) | {
                "matched": [{"path": path.decode("utf-8", errors="backslashreplace"), "hex": path.hex()}
                            for path in hit["matched"]],
                "kind": kind, "comparison": json.loads(detail), "sources": json.loads(sources),
            })
        return {"scope": self.scope, "matches": output, "verified": verified,
                "coverage": self.change_meta["coverage"],
                "limitation": "Exact changed-path overlap is not a fix or causality claim."}

    def read(self, number, family="issue", location="/value", offset=0, characters=12000):
        """Read one paged projection of original evidence, retaining its coverage status.

        Args:
            number: Repository-local issue or PR number.
            family: Exact archived fact family; issue also contains PR title and body.
            location: JSON Pointer into the original observation envelope.
            offset: Starting character offset in the selected string or JSON projection.
            characters: Maximum characters, with continuation returned explicitly.
        """
        if offset < 0 or characters < 1:
            raise ValueError("Invalid evidence page")
        source, document = self._source(number, family)
        result = {"scope": self.scope, "source": source, "pointer": location}
        if document is None:
            return result
        value = pointer(document, location)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        return result | {"text": text[offset:offset + characters], "offset": offset, "characters": len(text),
                         "next_offset": offset + characters if offset + characters < len(text) else None,
                         "encoding": "original_string" if isinstance(value, str) else "json_projection"}

    def tools(self):
        """Return additive offline tools; ordinary coding and live web tools remain available."""
        limit = {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}
        output = []
        for name, description, function, properties, required in [
            ("GitHubSearch", ("Search offline issue/PR titles, bodies, comments and reviews with an explicit "
             "FTS5 expression (quoted phrases, AND/OR/NOT). Returns BM25-ranked threads and source pointers."),
             self.search, {"query": {"type": "string"}, "limit": limit}, ["query"]),
            ("GitHubChanges", ("Find PRs whose native Git comparisons changed exact repository-relative paths. "
             "Proposal compares merge-base to head; landing compares first-parent to merged commit. "
             "Returns endpoint commits and observation pointers, ranked by matched-path count; not verified fixes."),
             self.changed, {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 30},
                            "kind": {"type": "string", "enum": ["proposal", "landing"]}, "limit": limit},
             ["paths", "kind"]),
            ("GitHubRead", ("Read original offline evidence for an issue/PR number and fact family. "
             "Families include issue (also PR title/body), issue-comments, pull, pull-reviews, pull-review-comments, "
             "pull-commits and pull-git. location is a JSON Pointer into the observation (e.g. /value/body). "
             "Character pagination applies to the original string or the displayed JSON projection; "
             "coverage distinguishes missing, incomplete and complete observations."),
             self.read, {"number": {"type": "integer", "minimum": 1},
                         "family": {"type": "string", "default": "issue"},
                         "location": {"type": "string", "default": "/value", "pattern": "^(?:/|$)"},
                         "offset": {"type": "integer", "minimum": 0, "default": 0},
                         "characters": {"type": "integer", "minimum": 1, "maximum": 30000, "default": 12000}},
             ["number"]),
        ]:
            async def invoke(arguments, function=function):
                return json.dumps(await self._work(lambda: function(**arguments)), ensure_ascii=False)

            output.append(Tool(name, description, {"type": "object", "properties": properties,
                                                  "required": required, "additionalProperties": False}, invoke))
        return output
