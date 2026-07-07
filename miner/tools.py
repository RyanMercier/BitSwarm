import ast
import contextvars
import os
import subprocess


# --- Configuration (set at runtime from subtask assignment) ---
# Uses contextvars for async safety  -  each concurrent miner gets its own config.

_repo_root_var = contextvars.ContextVar("repo_root", default="")
_allowed_files_var = contextvars.ContextVar("allowed_files", default=[])
_stub_test_files_var = contextvars.ContextVar("stub_test_files", default=[])
# Diff mode: when set to "diff", interface-stability checks compare
# the miner's new content against the TARGET STUB (the post-edit
# contract) instead of against the original file. In diff mode the
# whole point is that the public interface changes; the target stub
# defines the new shape, not the original.
_mode_var = contextvars.ContextVar("mode", default="scaffold")
_target_stubs_var = contextvars.ContextVar("target_stubs", default={})

BASH_TIMEOUT_SECONDS = 60
MAX_FILE_READ_BYTES = 512_000
MAX_FILE_WRITE_BYTES = 1_048_576


# --- Tool Schemas (Anthropic format) ---

TOOL_DEFINITIONS = [
    {
        "name": "file_read",
        "description": (
            "Read the contents of a file in the repository. "
            "Use this to read stub files, shared files, test files, "
            "and existing repo files for context. "
            "Returns the file content as a string. "
            "Fails if the file does not exist or exceeds size limits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from repository root to the file to read. "
                        "Example: 'auth/google_client.py' or 'tests/test_google_client.py'"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Optional byte offset to start reading from. Default 0. "
                        "Use for reading large files in chunks."
                    ),
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Optional maximum bytes to read. Default reads entire file up to size limit."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": (
            "Write content to a file. ONLY works for files in your allowed_files list. "
            "Writes the COMPLETE file content  -  this is a full replace, not a patch. "
            "The file must already exist (you are replacing NotImplementedError stubs). "
            "Fails if the path is not in allowed_files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from repository root. "
                        "Must be in your allowed_files list."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The complete file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bash",
        "description": (
            "Execute a shell command and return stdout and stderr. "
            "Use for: running pytest, searching files with grep/find, "
            "checking Python syntax, inspecting the repository. "
            "Commands run in the repository root directory. "
            "Network access is disabled. "
            "Commands time out after 60 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to execute. "
                        "For tests: 'pytest tests/test_file.py -v --tb=short' "
                        "For search: 'grep -rn pattern path/' "
                        "For structure: 'find . -name \"*.py\" -not -path \"./venv/*\"'"
                    ),
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List files and directories at a given path. Lightweight alternative "
            "to bash('ls'). Returns a flat listing with file sizes. "
            "Use to understand project structure or check what exists in a directory "
            "without a full bash invocation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from repository root. "
                        "Default '.' for repository root. "
                        "Example: 'auth/' or 'tests/'"
                    ),
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum directory depth to list. Default 2.",
                    "default": 2,
                },
            },
            "required": [],
        },
    },
]


# --- Pre-execution Validators ---

def validate_file_read(params):
    """Validate file_read before execution. Returns (ok, error_message)."""
    repo_root = _repo_root_var.get()
    path = params.get("path", "")
    abs_path = os.path.normpath(os.path.join(repo_root, path))

    if not abs_path.startswith(os.path.normpath(repo_root)):
        return False, f"Path traversal denied: {path}"

    if not os.path.isfile(abs_path):
        return False, f"File not found: {path}"

    size = os.path.getsize(abs_path)
    if size > MAX_FILE_READ_BYTES:
        return False, (
            f"File too large: {size} bytes (limit {MAX_FILE_READ_BYTES}). "
            f"Use offset and limit params to read in chunks."
        )

    return True, ""


def validate_file_write(params):
    """Validate file_write before execution. Enforces allowed_files scope."""
    repo_root = _repo_root_var.get()
    allowed_files = _allowed_files_var.get()
    path = params.get("path", "")
    content = params.get("content", "")
    abs_path = os.path.normpath(os.path.join(repo_root, path))

    if not abs_path.startswith(os.path.normpath(repo_root)):
        return False, f"Path traversal denied: {path}"

    # Scope check
    normalized_allowed = [
        os.path.normpath(os.path.join(repo_root, f)) for f in allowed_files
    ]
    if abs_path not in normalized_allowed:
        return False, (
            f"SCOPE VIOLATION: {path} is not in your allowed_files. "
            f"You may only modify: {allowed_files}. "
            f"This attempt has been logged. Modify only your assigned files."
        )

    # Size check
    if len(content.encode("utf-8")) > MAX_FILE_WRITE_BYTES:
        return False, f"Content too large: limit is {MAX_FILE_WRITE_BYTES} bytes."

    # Syntax check
    try:
        ast.parse(content)
    except SyntaxError as e:
        return False, (
            f"SYNTAX ERROR in your code: {e}. "
            f"Fix the syntax before writing. Line {e.lineno}: {e.text}"
        )

    # Public interface check. Two modes:
    #   scaffold mode: compare against the on-disk file (the stub). The
    #     miner may only implement existing public symbols; adding new
    #     ones is a contract violation.
    #   diff mode: compare against the TARGET STUB (the coordinator's
    #     post-edit contract). The miner is free to add public symbols
    #     listed in the target stub, since the target stub IS the spec.
    try:
        mode = _mode_var.get()
        if mode == "diff":
            target_stubs = _target_stubs_var.get() or {}
            # Look up the target stub for this file via its repo-relative
            # path. The path key is the same one used in modify_files.
            rel = os.path.relpath(abs_path, os.path.normpath(repo_root))
            target_stub = target_stubs.get(rel)
            if target_stub:
                target_public = _extract_public_names(target_stub)
                new_public = _extract_public_names(content)
                extra = new_public - target_public
                if extra:
                    return False, (
                        f"INTERFACE VIOLATION (diff mode): you added public "
                        f"symbols that are not in the target stub for {rel}: "
                        f"{extra}. The target stub is the spec; if you need a "
                        f"public symbol it must be declared there. Private "
                        f"helpers (prefixed with _) are allowed."
                    )
            # No target stub for this path -> skip the interface check
            # (the file is a new test file or other freely-writable
            # path the miner is permitted to edit).
        else:
            if os.path.isfile(abs_path):
                with open(abs_path, "r") as f:
                    original_content = f.read()
                original_public = _extract_public_names(original_content)
                new_public = _extract_public_names(content)
                added = new_public - original_public
                if added:
                    return False, (
                        f"INTERFACE VIOLATION: You added new public symbols that "
                        f"were not in the original stub: {added}. "
                        f"You may only implement existing functions/classes. "
                        f"Private helpers (prefixed with _) are allowed."
                    )
    except Exception:
        pass

    return True, ""


def _extract_public_names(source):
    """Extract public (non-underscore-prefixed) top-level names from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
    return names


BASH_BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "curl ",
    "wget ",
    "pip install",
    "apt ",
    "sudo ",
    "chmod ",
    "chown ",
    "mkfs",
    "dd if=",
    "> /dev/",
    "shutdown",
    "reboot",
    "kill -9",
    "pkill",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
]


def validate_bash(params):
    """Validate bash command before execution."""
    command = params.get("command", "")
    for pattern in BASH_BLOCKED_PATTERNS:
        if pattern in command.lower():
            return False, f"Blocked command pattern: {pattern}"
    return True, ""


def validate_list_files(params):
    """Validate list_files before execution."""
    repo_root = _repo_root_var.get()
    path = params.get("path", ".")
    abs_path = os.path.normpath(os.path.join(repo_root, path))

    if not abs_path.startswith(os.path.normpath(repo_root)):
        return False, f"Path traversal denied: {path}"

    if not os.path.isdir(abs_path):
        return False, f"Directory not found: {path}"

    return True, ""


# --- Tool Execution Functions ---

def execute_file_read(params):
    """Execute file_read and return content."""
    repo_root = _repo_root_var.get()
    path = params["path"]
    abs_path = os.path.normpath(os.path.join(repo_root, path))
    offset = params.get("offset", 0)
    limit = params.get("limit")

    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        if offset:
            f.seek(offset)
        content = f.read(limit) if limit else f.read()

    return content


def execute_file_write(params):
    """Execute file_write and return confirmation."""
    repo_root = _repo_root_var.get()
    path = params["path"]
    abs_path = os.path.normpath(os.path.join(repo_root, path))
    content = params["content"]

    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)

    line_count = content.count("\n") + 1
    return f"Written {len(content)} bytes ({line_count} lines) to {path}"


def execute_bash(params):
    """Execute the agent's bash command and return output.

    Model-written commands are the miner's largest attack surface, so
    execution routes through validator.sandbox: with BITSWARM_SANDBOX
    active (auto/docker) the command runs inside a network-less
    container with only the workspace mounted and an allowlisted
    environment; keys and the host filesystem are unreachable. Host
    mode runs ``sh -c <command>`` in the workspace, the same semantics
    the old shell=True call had. The validate_bash blocklist stays in
    front of both paths as defense in depth.
    """
    from validator.sandbox import run as sandboxed_run
    repo_root = _repo_root_var.get()
    command = params["command"]

    try:
        result = sandboxed_run(
            ["sh", "-c", command], repo_root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=BASH_TIMEOUT_SECONDS,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output += f"\n[exit code: {result.returncode}]"
        return output.strip()

    except subprocess.TimeoutExpired:
        return f"[TIMEOUT: command exceeded {BASH_TIMEOUT_SECONDS}s limit]"
    except Exception as e:
        return f"[ERROR: {e}]"


def execute_list_files(params):
    """Execute list_files and return directory listing."""
    repo_root = _repo_root_var.get()
    path = params.get("path", ".")
    max_depth = params.get("max_depth", 2)
    abs_path = os.path.normpath(os.path.join(repo_root, path))

    lines = []
    for root, dirs, files in os.walk(abs_path):
        depth = root.replace(abs_path, "").count(os.sep)
        if depth >= max_depth:
            dirs.clear()
            continue
        dirs[:] = [
            d for d in sorted(dirs)
            if not d.startswith(".") and d not in ("__pycache__", "node_modules", "venv")
        ]
        indent = "  " * depth
        rel_root = os.path.relpath(root, repo_root)
        lines.append(f"{indent}{rel_root}/")
        for f in sorted(files):
            if f.startswith("."):
                continue
            fpath = os.path.join(root, f)
            size = os.path.getsize(fpath)
            lines.append(f"{indent}  {f}  ({size} bytes)")

    return "\n".join(lines) if lines else "(empty directory)"


# --- Tool Router ---

TOOL_REGISTRY = {
    "file_read": {"validate": validate_file_read, "execute": execute_file_read},
    "file_write": {"validate": validate_file_write, "execute": execute_file_write},
    "bash": {"validate": validate_bash, "execute": execute_bash},
    "list_files": {"validate": validate_list_files, "execute": execute_list_files},
}


def configure(repo_root, allowed_files, stub_test_files=None,
               mode: str = "scaffold", target_stubs: dict | None = None):
    """Set the runtime configuration for the tools module (async-safe).

    ``mode`` selects scaffold-mode (default) or diff-mode behavior for
    the file_write interface check. ``target_stubs`` is the
    {modify_file_path: target_stub_source} dict in diff mode; ignored
    in scaffold mode.
    """
    _repo_root_var.set(repo_root)
    _allowed_files_var.set(allowed_files)
    _stub_test_files_var.set(stub_test_files or [])
    _mode_var.set(mode)
    _target_stubs_var.set(target_stubs or {})


def run_tool(name, params):
    """
    Main tool execution entry point.
    Returns {"success": bool, "output": str}
    """
    if name not in TOOL_REGISTRY:
        return {"success": False, "output": f"Unknown tool: {name}"}

    tool = TOOL_REGISTRY[name]

    ok, error = tool["validate"](params)
    if not ok:
        return {"success": False, "output": error}

    try:
        output = tool["execute"](params)
        return {"success": True, "output": output}
    except Exception as e:
        return {"success": False, "output": f"Tool execution error: {e}"}
