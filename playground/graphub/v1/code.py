"""Route native CBM queries to bounded, on-demand projections of archived snapshots.

Graph relationships come only from gh_puller.codebase; CBM owns search and graph
algorithms. Git supplies the matching source tree. Coverage records and embeddings
are not present in these archives, so every result carries that limitation. The
working checkout used by Bash is independent of these immutable query snapshots.
"""

import asyncio
import json
import os
import sqlite3
import time
from collections import OrderedDict
from contextlib import ExitStack
from copy import deepcopy
from hashlib import new, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from gh_puller.codebase import Archive, resolve_cbm_binary
from gh_puller.codebase.archive import TreeRef, graph_digest
from gh_puller.codebase.cbm_transport import PersistentMCPTransport
from gh_puller.codebase.git_tree import materialize_full
from playground.graphub.change_git import run
from playground.graphub.stack_search import restore

from .agent import Tool

READ_TOOLS = frozenset({"search_graph", "query_graph", "trace_path", "get_code_snippet", "get_graph_schema",
                        "get_architecture", "search_code", "list_projects", "index_status", "check_index_coverage"})


def frozen_archive(path, scope):
    archive = Archive(path, allow_incomplete=True)
    identifiers = archive.commit_ids()[:scope["graph_count"]]
    if len(identifiers) != scope["graph_count"]:
        raise ValueError("The frozen graph prefix is unavailable")
    identities = []
    for identifier in sorted(identifiers):
        item = archive.manifest(identifier)
        if graph_digest(TreeRef.from_json(item.get("node_root")),
                        TreeRef.from_json(item.get("edge_root"))) != item["graph_digest"]:
            raise ValueError("Archived graph roots differ from their manifest")
        identities.append((identifier, item["graph_digest"], item["parents"]))
    if sha256(json.dumps(identities).encode()).hexdigest() != scope["graph_identity_digest"]:
        raise ValueError("The graph prefix differs from its frozen identity")
    return archive, frozenset(identifiers)


def verify_source(git, commit, tree):
    """Reject archive attribute transformations before exposing source through CBM.

    Args:
        git: Canonical local Git object store.
        commit: Exact commit whose blob identities are the comparison oracle.
        tree: Newly materialized source directory; submodules remain external.
    """
    listing = run(git, "ls-tree", "-r", "-z", commit)
    listing.check_returncode()
    files, submodules = 0, []
    for record in listing.stdout.split(b"\0")[:-1]:
        metadata, filename = record.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        path = tree / os.fsdecode(filename)
        if kind == b"commit":
            submodules.append(os.fsdecode(filename))
            continue
        if not path.resolve().is_relative_to(tree.resolve()):
            raise ValueError(f"Source path escapes its archived tree: {filename!r}")
        data = os.fsencode(os.readlink(path)) if mode == b"120000" else path.read_bytes()
        digest = new("sha1" if len(oid) == 40 else "sha256", usedforsecurity=False)
        digest.update(b"blob " + str(len(data)).encode() + b"\0" + data)
        if digest.hexdigest().encode() != oid:
            raise ValueError(f"Materialized source differs from Git blob: {filename!r}")
        files += 1
    return {"verified_blobs": files, "external_submodules": submodules}


class Code:
    def __init__(self, archive: Path, git: Path, scope: dict, directory: Path, revision: str, *, capacity: int = 2):
        """Bind a repository's native graph tools without copying its history.

        Args:
            archive: Append-only graph archive, read through a captured commit prefix.
            git: Canonical local Git object store; no network fetch is performed.
            scope: Frozen graph_count, graph_identity_digest and cbm_sha256 contract.
            directory: New run-owned directory for disposable projections and source trees.
            revision: Default Git revision for every tool. Calls can select another revision.
            capacity: Maximum resident projected snapshots, evicted least-recently-used.
        """
        if capacity < 1:
            raise ValueError("Snapshot capacity must be positive")
        self.archive, self.identifiers = frozen_archive(archive, scope)
        self.git, self.directory, self.revision, self.capacity = git.resolve(), directory.resolve(), revision, capacity
        self.binary = resolve_cbm_binary()
        if self.binary.sha256 != scope["cbm_sha256"]:
            raise ValueError("CBM differs from the frozen query binary")
        self.snapshots = OrderedDict()
        self.records = []
        self.closed = False
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        try:
            snapshot = await self._work(self.snapshot, self.revision)
            self.definitions = await self._work(snapshot.engine.list_tools)
        except BaseException:  # Partially prepared snapshots still belong to this run.
            await asyncio.to_thread(self.close)
            raise
        return self

    async def __aexit__(self, *_exc):
        await asyncio.to_thread(self.close)

    async def _work(self, function, *arguments):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(function, *arguments))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # A worker may still own a native process or be creating a snapshot.
                try:
                    await task
                finally:
                    await asyncio.to_thread(self.close)
                    raise exc

    def close(self):
        self.closed = True
        while self.snapshots:
            _, snapshot = self.snapshots.popitem(last=False)
            snapshot.resources.close()

    def snapshot(self, revision):
        if self.closed:
            raise RuntimeError("Code snapshot session is closed")
        resolved = run(self.git, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
        resolved.check_returncode()
        commit = resolved.stdout.decode().strip()
        if commit not in self.identifiers:
            raise ValueError(f"No graph for {commit} in the frozen archive; use ordinary source tools")
        if commit in self.snapshots:
            self.snapshots.move_to_end(commit)
            return self.snapshots[commit]
        if len(self.snapshots) == self.capacity:
            _, old = self.snapshots.popitem(last=False)
            old.resources.close()
        started = time.monotonic()
        resources = ExitStack()
        project = "snapshot_" + commit
        try:
            cache = Path(resources.enter_context(TemporaryDirectory(prefix="snapshot-", dir=self.directory)))
            # Unix socket paths must stay short even when the run directory is deeply nested.
            runtime = resources.enter_context(TemporaryDirectory(prefix="graphub-cbm-", dir="/tmp"))
            tree = cache / "source"
            materialize_full(self.git, commit, tree)
            source = verify_source(self.git, commit, tree)
            projection = restore(self.archive.load_rows(commit), cache, project)
            with sqlite3.connect(cache / f"{project}.db") as db:
                db.execute("UPDATE projects SET root_path=? WHERE name=?", (str(tree), project))
            self.binary.verify_unchanged()
            engine = PersistentMCPTransport(
                self.binary.path, cache, 45, SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None),
                {"CBM_RUNTIME_DIR": runtime},
            )
            resources.callback(engine.close)
        except BaseException:  # Cleanup includes native startup failures, not just query failures.
            resources.close()
            raise
        identity = {"commit": commit, "graph_digest": self.archive.manifest(commit)["graph_digest"],
                    "project": project, "coverage_records": "not_archived", "embeddings": "not_archived",
                    "source": source,
                    "dangling_edges_omitted": projection["source_dangling_edges"]}
        self.records.append(identity | {"projection": projection, "seconds": time.monotonic() - started})
        snapshot = SimpleNamespace(engine=engine, identity=identity, resources=resources,
                                   cache=cache, runtime=Path(runtime))
        self.snapshots[commit] = snapshot
        return snapshot

    def query(self, name, arguments):
        if name not in READ_TOOLS:
            raise ValueError("Only native static analysis tools are exposed")
        arguments = dict(arguments)
        snapshot = self.snapshot(arguments.pop("revision", self.revision))
        if name != "list_projects":
            arguments["project"] = snapshot.identity["project"]
        response = snapshot.engine.call_tool(name, arguments)
        return json.dumps({"snapshot": snapshot.identity, "native_result": response}, ensure_ascii=False)

    def tools(self):
        """Return native read tools with explicit, defaultable revision routing."""
        output = []
        for definition in self.definitions:
            if definition["name"] not in READ_TOOLS:
                continue
            name = definition["name"]
            schema = deepcopy(definition["inputSchema"])
            schema.setdefault("properties", {})["revision"] = {
                "type": "string", "description": "Git commit or ref. Omission uses the user input's code version.",
            }
            if "project" in schema["properties"]:
                del schema["properties"]["project"]
                schema["required"] = [key for key in schema.get("required", []) if key != "project"]

            async def invoke(arguments, name=name):
                return await self._work(self.query, name, arguments)

            output.append(Tool(name, definition["description"] +
                               " Archived snapshot: coverage records and embeddings are unavailable; "
                               "missing graph results do not prove absence. Select a version with revision; "
                               "project is supplied automatically.", schema, invoke))
        return output
