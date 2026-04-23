import ast
import os
import subprocess
import sys
import tempfile
import shutil


# Standard library module names (common ones)
STDLIB_TOP_LEVEL = {
    "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio", "asyncore",
    "atexit", "base64", "bdb", "binascii", "binhex", "bisect", "builtins",
    "bz2", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code",
    "codecs", "codeop", "collections", "colorsys", "compileall", "concurrent",
    "configparser", "contextlib", "contextvars", "copy", "copyreg", "cProfile",
    "crypt", "csv", "ctypes", "curses", "dataclasses", "datetime", "dbm",
    "decimal", "difflib", "dis", "distutils", "doctest", "email", "encodings",
    "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput", "fnmatch",
    "fractions", "ftplib", "functools", "gc", "getopt", "getpass", "gettext",
    "glob", "grp", "gzip", "hashlib", "heapq", "hmac", "html", "http",
    "idlelib", "imaplib", "imghdr", "imp", "importlib", "inspect", "io",
    "ipaddress", "itertools", "json", "keyword", "lib2to3", "linecache",
    "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math",
    "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc", "nis",
    "nntplib", "numbers", "operator", "optparse", "os", "ossaudiodev",
    "pathlib", "pdb", "pickle", "pickletools", "pipes", "pkgutil", "platform",
    "plistlib", "poplib", "posix", "posixpath", "pprint", "profile", "pstats",
    "pty", "pwd", "py_compile", "pyclbr", "pydoc", "queue", "quopri",
    "random", "re", "readline", "reprlib", "resource", "rlcompleter", "runpy",
    "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil",
    "signal", "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver",
    "sqlite3", "ssl", "stat", "statistics", "string", "stringprep", "struct",
    "subprocess", "sunau", "symtable", "sys", "sysconfig", "syslog", "tabnanny",
    "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap",
    "threading", "time", "timeit", "tkinter", "token", "tokenize", "trace",
    "traceback", "tracemalloc", "tty", "turtle", "turtledemo", "types",
    "typing", "unicodedata", "unittest", "urllib", "uu", "uuid", "venv",
    "warnings", "wave", "weakref", "webbrowser", "winreg", "winsound",
    "wsgiref", "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport",
    "zlib", "_thread",
}


def extract_imports(source):
    """Extract all imported module names from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


def resolves(module, repo_root, shared_files, stub_files=None, requirements=None):
    """
    Check if a module import can be resolved to:
    - A stdlib module
    - A file in the repo
    - A shared file being created
    - A stub file being created
    - A package in requirements
    """
    top_level = module.split(".")[0]

    # Standard library
    if top_level in STDLIB_TOP_LEVEL:
        return True

    # Common third-party packages (check requirements)
    if requirements:
        # Normalize package names for comparison
        req_names = set()
        for r in requirements:
            req_names.add(r.lower().replace("-", "_"))
        if top_level.lower().replace("-", "_") in req_names:
            return True

    # Well-known packages that map to different import names
    known_mappings = {
        "flask": "flask", "flask_sqlalchemy": "flask-sqlalchemy",
        "werkzeug": "flask", "jinja2": "flask",
        "sqlalchemy": "flask-sqlalchemy",
        "pytest": "pytest", "_pytest": "pytest",
        "requests": "requests",
        "google": "google-auth",
        "pydantic": "pydantic",
        "authlib": "authlib",
        # numpy/PIL ecosystem
        "numpy": "numpy", "np": "numpy",
        "PIL": "Pillow", "PIL": "pillow",
        "cv2": "opencv-python",
        "sklearn": "scikit-learn",
        "scipy": "scipy",
        "matplotlib": "matplotlib",
    }
    if top_level in known_mappings:
        return True

    # Also resolve if the top-level name appears as a known package alias
    # e.g. import PIL when requirements has "Pillow"
    PIL_ALIASES = {"PIL", "pil"}
    NUMPY_ALIASES = {"numpy", "np"}
    if top_level in PIL_ALIASES:
        for r in (requirements or []):
            if r.lower().startswith("pillow"):
                return True
    if top_level in NUMPY_ALIASES:
        for r in (requirements or []):
            if r.lower().startswith("numpy"):
                return True

    # Check existing repo files
    parts = module.split(".")
    # Try module.py
    mod_path = os.path.join(repo_root, *parts) + ".py"
    if os.path.isfile(mod_path):
        return True
    # Try package/__init__.py
    pkg_path = os.path.join(repo_root, *parts, "__init__.py")
    if os.path.isfile(pkg_path):
        return True
    # Try parent module
    if len(parts) > 1:
        parent_path = os.path.join(repo_root, *parts[:-1]) + ".py"
        if os.path.isfile(parent_path):
            return True
        parent_pkg = os.path.join(repo_root, *parts[:-1], "__init__.py")
        if os.path.isfile(parent_pkg):
            return True

    # Check shared files
    if shared_files:
        for path in shared_files:
            # Convert file path to module path
            if path.endswith(".py"):
                mod_from_path = path[:-3].replace("/", ".").replace("\\", ".")
                if mod_from_path.endswith(".__init__"):
                    mod_from_path = mod_from_path[:-9]
                if module == mod_from_path or module.startswith(mod_from_path + "."):
                    return True

    # Check stub files
    if stub_files:
        for path in stub_files:
            if path.endswith(".py"):
                mod_from_path = path[:-3].replace("/", ".").replace("\\", ".")
                if mod_from_path.endswith(".__init__"):
                    mod_from_path = mod_from_path[:-9]
                if module == mod_from_path or module.startswith(mod_from_path + "."):
                    return True

    return False


def extract_defined_names(source):
    """
    Extract all top-level names defined in a Python source file.
    Returns {name: info_dict} where info_dict describes the kind and signature.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    names = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            n_required = len(args.args) - len(args.defaults)
            names[node.name] = {
                "kind": "function",
                "min_args": n_required,
                "max_args": len(args.args),
                "arg_names": [a.arg for a in args.args],
            }
        elif isinstance(node, ast.ClassDef):
            methods = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = item.args
                    non_self = [a for a in args.args if a.arg not in ("self", "cls")]
                    n_required = len(non_self) - len(args.defaults)
                    methods[item.name] = {
                        "kind": "method",
                        "min_args": max(0, n_required),
                        "max_args": len(non_self),
                        "arg_names": [a.arg for a in non_self],
                        "has_varargs": args.vararg is not None,
                        "has_kwargs": args.kwarg is not None,
                    }
            names[node.name] = {
                "kind": "class",
                "methods": methods,
            }
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = {"kind": "variable"}

    return names


def check_interface_contracts(shared_files, stub_files, stub_test_files, integration_test_files,
                              repo_path=None):
    """
    Phase 1.5: Verify that cross-file interfaces are consistent.

    Catches bugs like:
    - shared main.py imports 'render_scene' from renderer, but renderer only defines 'Renderer' class
    - integration test imports 'load_scene' but scene.py defines 'Scene.load_from_file'
    - stub A calls stub B's method with wrong number of args
    """
    errors = []

    # Build registry: dotted_module_path -> {name: info}
    all_source = {**shared_files, **stub_files}
    registry = {}

    for fpath, content in all_source.items():
        if not fpath.endswith(".py"):
            continue
        dotted = fpath[:-3].replace("/", ".").replace("\\", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[:-9]
        registry[dotted] = extract_defined_names(content)

    # Check 1: Every "from module import name" references a name that exists in that module
    all_code = {**shared_files, **stub_files, **stub_test_files, **integration_test_files}

    # Also include existing repo .py files that import from any registered module.
    # This catches e.g. main.py importing render_scene from a stub module.
    if repo_path:
        stub_modules = set(registry.keys())
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", "node_modules")]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, repo_path)
                if rel in all_code:
                    continue  # already included
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    # Only include if it imports from a stub/shared module
                    for mod in extract_imports(content):
                        if mod in stub_modules or mod.split(".")[0] in {m.split(".")[0] for m in stub_modules}:
                            all_code[rel] = content
                            break
                except OSError:
                    pass

    for fpath, content in all_code.items():
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                if module not in registry:
                    continue  # external module, handled by import resolution check

                module_names = registry[module]
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if alias.name not in module_names:
                        available = sorted(n for n in module_names if not n.startswith("_"))
                        errors.append(
                            f"Interface mismatch in {fpath}: imports '{alias.name}' from "
                            f"'{module}', but '{alias.name}' is not defined there. "
                            f"Defined names: {available}. Either add '{alias.name}' to "
                            f"the module or fix the import."
                        )

    # Check 2: Cross-stub method call arity
    # For each file, resolve imports to known classes, then check method calls
    for fpath, content in all_code.items():
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        # Build local name -> class info map from imports in this file
        local_classes = {}  # local_name -> (module, class_name, class_info)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module in registry:
                for alias in node.names:
                    name = alias.name
                    local_name = alias.asname or name
                    info = registry[node.module].get(name)
                    if info and info["kind"] == "class":
                        local_classes[local_name] = (node.module, name, info)

        # Find direct class instantiations: ClassName(args...)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in local_classes:
                module, cls_name, cls_info = local_classes[node.func.id]
                init = cls_info["methods"].get("__init__")
                if init and not init.get("has_varargs") and not init.get("has_kwargs"):
                    n_args = len(node.args) + len(node.keywords)
                    if n_args < init["min_args"] or n_args > init["max_args"]:
                        errors.append(
                            f"Arity mismatch in {fpath}: '{cls_name}(...)' called with "
                            f"{n_args} args, but {module}.{cls_name}.__init__ expects "
                            f"{init['min_args']}-{init['max_args']} args "
                            f"(params: {init['arg_names']}). Fix the call or the signature."
                        )

    return errors


def check_no_circular_deps(subtasks):
    """Check that the dependency graph has no cycles. Returns list of errors."""
    # Build adjacency list
    graph = {}
    for st in subtasks:
        sid = st["subtask_id"]
        graph[sid] = st.get("dependencies", [])

    # DFS cycle detection
    visited = set()
    in_stack = set()
    errors = []

    def dfs(node, path):
        if node in in_stack:
            cycle = path[path.index(node):] + [node]
            errors.append(f"Circular dependency: {' -> '.join(cycle)}")
            return
        if node in visited:
            return
        visited.add(node)
        in_stack.add(node)
        for dep in graph.get(node, []):
            dfs(dep, path + [node])
        in_stack.discard(node)

    for node in graph:
        if node not in visited:
            dfs(node, [])

    return errors


def run_pytest(test_file, repo_root):
    """Run a single test file and return the subprocess result."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-x", "--tb=short", "-q"],
        capture_output=True, text=True, cwd=repo_root, timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def validate_decomposition(decomposition, repo_path):
    """
    Run all validation checks on the decomposition.
    Returns a list of error strings (empty means valid).
    """
    errors = []

    shared_files = decomposition.get("shared_files", {})
    stub_files = decomposition.get("stub_files", {})
    stub_test_files = decomposition.get("stub_test_files", {})
    integration_test_files = decomposition.get("integration_test_files", {})
    subtasks = decomposition.get("subtasks", [])
    requirements_additions = decomposition.get("requirements_additions", [])

    # Read existing requirements
    req_path = os.path.join(repo_path, "requirements.txt")
    existing_reqs = []
    if os.path.isfile(req_path):
        with open(req_path) as f:
            existing_reqs = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    all_reqs = existing_reqs + requirements_additions

    all_file_contents = {
        **shared_files,
        **stub_files,
        **stub_test_files,
        **integration_test_files,
    }

    # Check 1: All files parse as valid Python
    for path, content in all_file_contents.items():
        try:
            ast.parse(content)
        except SyntaxError as e:
            errors.append(f"SyntaxError in {path} line {e.lineno}: {e.text}")

    # Check 1b: Catch common alias-without-import bugs (e.g. using np. without importing numpy as np)
    ALIAS_CHECKS = [
        ("np.", "import numpy as np", "numpy"),
        ("pd.", "import pandas as pd", "pandas"),
    ]
    for path, content in all_file_contents.items():
        for alias_prefix, required_import, pkg in ALIAS_CHECKS:
            if alias_prefix in content and required_import not in content:
                errors.append(
                    f"{path} uses '{alias_prefix}' but is missing '{required_import}' — "
                    f"add 'import {pkg} as {alias_prefix[:-1]}' at the top of the file"
                )

    # Build a lookup: bare module name → full dotted module path, from all known files
    # e.g. 'vector' → 'mypackage.vector' when mypackage/vector.py exists
    all_known_files = set(shared_files) | set(stub_files)
    bare_to_full = {}
    for fpath in all_known_files:
        if fpath.endswith(".py"):
            dotted = fpath[:-3].replace("/", ".").replace("\\", ".")
            if dotted.endswith(".__init__"):
                dotted = dotted[:-9]
            bare = dotted.split(".")[-1]
            if bare not in bare_to_full:
                bare_to_full[bare] = dotted

    def import_fix_hint(module):
        top = module.split(".")[0]
        if top in bare_to_full:
            return f" -- did you mean '{bare_to_full[top]}'? Use full package path e.g. 'from {bare_to_full[top]} import ...'"
        return " -- define it in a shared file, use the full package path (e.g. 'from mypackage.module import MyClass'), or add it to requirements_additions"

    # Check 2: All imports in stub files resolve
    for path, content in stub_files.items():
        for module in extract_imports(content):
            if not resolves(module, repo_path, shared_files, stub_files, all_reqs):
                errors.append(
                    f"Unresolved import in {path}: '{module}'"
                    + import_fix_hint(module)
                )

    # Check 3: All imports in test files resolve
    for path, content in stub_test_files.items():
        for module in extract_imports(content):
            if not resolves(module, repo_path, shared_files, stub_files, all_reqs):
                errors.append(
                    f"Unresolved import in test {path}: '{module}'"
                    + import_fix_hint(module)
                )

    # Check 3b: Existing repo files that import from stub/shared packages must still resolve
    # This catches e.g. main.py importing from raytracer.renderer when no renderer stub exists
    all_scaffolded = set(shared_files) | set(stub_files)
    scaffolded_packages = set()
    for fpath in all_scaffolded:
        parts = fpath.split("/")
        if len(parts) > 1:
            scaffolded_packages.add(parts[0])

    if scaffolded_packages:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", "node_modules")]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, repo_path)
                if rel in all_file_contents:
                    continue  # already checked above
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                for module in extract_imports(content):
                    top = module.split(".")[0]
                    if top not in scaffolded_packages:
                        continue  # not importing from our packages
                    if not resolves(module, repo_path, shared_files, stub_files, all_reqs):
                        errors.append(
                            f"Existing repo file '{rel}' imports '{module}' which will not "
                            f"exist after scaffolding. Either create a stub/shared file for "
                            f"this module, or update '{rel}' in shared_files to use the "
                            f"correct import path."
                        )

    # Check 4: No file path overlaps between subtasks
    all_paths = []
    for st in subtasks:
        for f in st.get("stub_files", []):
            if f in all_paths:
                errors.append(f"File path overlap: {f} assigned to multiple subtasks")
            all_paths.append(f)

    # Check 5: Complexity weights sum to 1.0
    total_weight = sum(st.get("complexity_weight", 0) for st in subtasks)
    if abs(total_weight - 1.0) > 0.01:
        errors.append(f"Complexity weights sum to {total_weight}, expected 1.0")

    # Check 6: No circular dependencies
    errors.extend(check_no_circular_deps(subtasks))

    # Note: Check 6b (dependency fan-out) removed — the coordinator prompt already
    # advises putting widely-depended-on modules in shared_files. Hard-failing here
    # burned retries on structural reshuffling instead of fixing real interface bugs.

    # Check 7: Each subtask has at least one stub test file AND that file has content
    provided_stubs = set(stub_files.keys())
    provided_tests = set(stub_test_files.keys())

    for st in subtasks:
        sid = st["subtask_id"]

        if not st.get("stub_test_files"):
            errors.append(f"Subtask '{sid}' has no stub test files")
            continue

        # Every stub file listed in the subtask must have content in the stub_files dict
        for f in st.get("stub_files", []):
            if f not in provided_stubs:
                errors.append(
                    f"Subtask '{sid}' lists stub file '{f}' but its content is missing "
                    f"from the stub_files dict. You must provide the full file content "
                    f"(with NotImplementedError bodies) for every stub file you list."
                )

        # Every test file listed in the subtask must have content in the stub_test_files dict
        for f in st.get("stub_test_files", []):
            if f not in provided_tests:
                errors.append(
                    f"Subtask '{sid}' lists test file '{f}' but its content is missing "
                    f"from the stub_test_files dict. You must provide the full test file "
                    f"content for every test file you list."
                )

    # Check 8: Phase 1.5 — Interface contract verification
    # Verify that every cross-file import references a name that actually exists
    # in the target module, and that class instantiations match constructor arity.
    if not errors:
        errors.extend(check_interface_contracts(
            shared_files, stub_files, stub_test_files, integration_test_files,
            repo_path=repo_path,
        ))

    # Check 9: Write scaffolding to disk and verify stub tests FAIL
    # (only if no prior errors, since broken syntax would crash pytest)
    if not errors:
        errors.extend(verify_stub_tests_fail(decomposition, repo_path, subtasks))

    return errors


def verify_stub_tests_fail(decomposition, repo_path, subtasks):
    """Write scaffolding and verify that stub tests fail (stubs raise NotImplementedError)."""
    errors = []
    shared_files = decomposition.get("shared_files", {})
    stub_files = decomposition.get("stub_files", {})
    stub_test_files = decomposition.get("stub_test_files", {})
    integration_test_files = decomposition.get("integration_test_files", {})
    requirements_additions = decomposition.get("requirements_additions", [])

    # Write all files to disk
    all_to_write = {**shared_files, **stub_files, **stub_test_files, **integration_test_files}
    for path, content in all_to_write.items():
        full_path = os.path.join(repo_path, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)

    # Ensure __init__.py files exist for all packages
    for path in all_to_write:
        parts = path.split("/")
        for i in range(1, len(parts)):
            init_path = os.path.join(repo_path, *parts[:i], "__init__.py")
            if not os.path.isfile(init_path):
                with open(init_path, "w") as f:
                    f.write("")

    # Update requirements.txt
    if requirements_additions:
        req_path = os.path.join(repo_path, "requirements.txt")
        if not os.path.isfile(req_path):
            with open(req_path, "w") as f:
                f.write("")
        with open(req_path, "r") as f:
            existing = f.read()
        with open(req_path, "a") as f:
            for req in requirements_additions:
                if req not in existing:
                    f.write(f"{req}\n")

    # Install any new requirements
    if requirements_additions:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q"] + requirements_additions,
            capture_output=True, cwd=repo_path,
        )

    # Run stub tests and verify they FAIL
    for st in subtasks:
        for test_file in st.get("stub_test_files", []):
            try:
                result = run_pytest(test_file, repo_path)
                if result.returncode == 0:
                    errors.append(
                        f"Stub test {test_file} PASSED on scaffolding -- "
                        f"tests should FAIL on NotImplementedError stubs. "
                        f"The test is probably a no-op or doesn't call the stub."
                    )
            except subprocess.TimeoutExpired:
                errors.append(f"Stub test {test_file} timed out")
            except Exception as e:
                errors.append(f"Error running stub test {test_file}: {e}")

    return errors
