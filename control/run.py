"""
BitSwarm Control Run
====================
Sends the exact same task to Claude in a single API call (no multi-agent pipeline).
Use this to compare against the BitSwarm orchestrator run:

  python control/run.py                   # uses ../spec.txt, saves to control/output/
  python control/run.py --spec my.txt     # custom spec
  python control/run.py --output /path    # custom output dir

The output directory will contain:
  control/output/
    response_raw.txt       -- Claude's full raw response
    files/                 -- all source files Claude wrote, ready to run
      main.py
      raytracer/...
      scene.json           -- copied from target repo
      requirements.txt
    cost_report.txt        -- token counts and estimated cost

To run the generated code:
  cd control/output/files
  pip install -r requirements.txt
  python main.py           -- renders output.png
"""

import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, ROOT)

import anthropic
from config import ANTHROPIC_API_KEY, COORDINATOR_MODEL


# Pricing for claude-sonnet-4 (per million tokens, as of early 2026)
# Update these if pricing changes.
INPUT_COST_PER_M  = 3.00   # USD per 1M input tokens
OUTPUT_COST_PER_M = 15.00  # USD per 1M output tokens


SYSTEM_PROMPT = """\
You are an expert Python developer. You will be given a feature specification and
an existing repository. Your job is to write ALL the code needed to implement the
feature completely and correctly.

Output ONLY file contents. Use this exact format for every file you create or modify:

<file path="relative/path/to/file.py">
# full file content here
</file>

Rules:
- Include every file needed for the implementation to work.
- Do not omit any file, even if it seems trivial (e.g. __init__.py).
- Do not include prose, explanations, or commentary outside the <file> tags.
- The code must be complete and runnable — no TODOs, no placeholders.
- Use only packages available in the requirements.txt you write.
- Every Python file must be syntactically valid.
"""


def build_prompt(spec, target_repo_path):
    """Build the full prompt including repo context and feature spec."""
    lines = []

    # Include existing repo files
    lines.append("## Existing Repository\n")
    for fname in sorted(os.listdir(target_repo_path)):
        fpath = os.path.join(target_repo_path, fname)
        if os.path.isfile(fpath) and not fname.startswith("."):
            with open(fpath) as f:
                content = f.read()
            lines.append(f"### {fname}\n```\n{content}\n```\n")

    # Include tests
    tests_dir = os.path.join(target_repo_path, "tests")
    if os.path.isdir(tests_dir):
        lines.append("### tests/\n")
        for fname in sorted(os.listdir(tests_dir)):
            fpath = os.path.join(tests_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath) as f:
                    content = f.read()
                lines.append(f"#### tests/{fname}\n```\n{content}\n```\n")

    lines.append("---\n")
    lines.append("## Feature Specification\n")
    lines.append(spec)
    lines.append("\n---\n")
    lines.append("Implement the feature completely. Output every file using the <file path=\"...\"> format.")

    return "\n".join(lines)


def parse_files_from_response(response_text):
    """Extract file path -> content pairs from the model's response."""
    pattern = r'<file path="([^"]+)">(.*?)</file>'
    matches = re.findall(pattern, response_text, re.DOTALL)
    files = {}
    for path, content in matches:
        # Strip leading/trailing whitespace but preserve internal newlines
        files[path.strip()] = content.strip("\n")
    return files


def write_output(files, target_repo_path, output_dir):
    """Write all extracted files to output_dir/files/, plus copy static assets."""
    files_dir = os.path.join(output_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    # Copy scene.json and any other static assets from target repo
    for fname in os.listdir(target_repo_path):
        src = os.path.join(target_repo_path, fname)
        if os.path.isfile(src) and fname not in files:
            import shutil
            shutil.copy2(src, os.path.join(files_dir, fname))

    # Write all generated files
    written = []
    for path, content in files.items():
        full_path = os.path.join(files_dir, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        written.append(path)

    return written


def compute_cost(input_tokens, output_tokens):
    input_cost  = (input_tokens  / 1_000_000) * INPUT_COST_PER_M
    output_cost = (output_tokens / 1_000_000) * OUTPUT_COST_PER_M
    return input_cost, output_cost, input_cost + output_cost


def write_cost_report(output_dir, input_tokens, output_tokens, elapsed_seconds, files_written):
    input_cost, output_cost, total_cost = compute_cost(input_tokens, output_tokens)

    lines = [
        "BitSwarm Control Run — Cost Report",
        "=" * 40,
        f"Input tokens:   {input_tokens:>10,}    ${input_cost:>8.4f}",
        f"Output tokens:  {output_tokens:>10,}    ${output_cost:>8.4f}",
        f"Total cost:     {'':>10}    ${total_cost:>8.4f}",
        f"Elapsed:        {elapsed_seconds:.1f}s",
        f"API calls:      1",
        "",
        f"Files written ({len(files_written)}):",
    ]
    for f in sorted(files_written):
        lines.append(f"  {f}")

    report = "\n".join(lines)
    print("\n" + report)

    report_path = os.path.join(output_dir, "cost_report.txt")
    with open(report_path, "w") as f:
        f.write(report + "\n")

    return total_cost


def run(spec, target_repo_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print("BITSWARM CONTROL RUN  (single API call)")
    print(f"{'='*60}")
    print(f"  Model:      {COORDINATOR_MODEL}")
    print(f"  Output dir: {output_dir}")

    prompt = build_prompt(spec, target_repo_path)

    print(f"\n  Prompt length: {len(prompt):,} chars")
    print("  Calling Claude API...")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Use streaming — required for large responses (64k max_tokens can exceed 10min timeout)
    start = time.time()
    response_text = ""
    input_tokens = 0
    output_tokens = 0
    with client.messages.stream(
        model=COORDINATOR_MODEL,
        max_tokens=64000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text_chunk in stream.text_stream:
            response_text += text_chunk
            if len(response_text) % 2000 < len(text_chunk):
                print(".", end="", flush=True)
        final = stream.get_final_message()
        input_tokens  = final.usage.input_tokens
        output_tokens = final.usage.output_tokens
    elapsed = time.time() - start
    print()  # newline after dots

    print(f"  Done in {elapsed:.1f}s")
    print(f"  Tokens: {input_tokens:,} in / {output_tokens:,} out")

    # Save raw response
    raw_path = os.path.join(output_dir, "response_raw.txt")
    with open(raw_path, "w") as f:
        f.write(response_text)
    print(f"  Raw response saved: {raw_path}")

    # Parse and write files
    files = parse_files_from_response(response_text)
    if not files:
        print("\n  WARNING: No <file path=\"...\"> blocks found in response.")
        print("  Check response_raw.txt — Claude may have used a different format.")
    else:
        written = write_output(files, target_repo_path, output_dir)
        print(f"\n  Files extracted and written ({len(written)}):")
        for path in sorted(written):
            print(f"    {path}")

        files_dir = os.path.join(output_dir, "files")
        print(f"\n  To run the result:")
        print(f"    cd {files_dir}")
        print(f"    pip install -r requirements.txt")
        print(f"    python main.py")

    # Cost report
    write_cost_report(output_dir, input_tokens, output_tokens, elapsed,
                      list(files.keys()) if files else [])


def main():
    parser = argparse.ArgumentParser(description="BitSwarm Control Run — single API call")
    parser.add_argument("--spec",   default=os.path.join(ROOT, "spec.txt"))
    parser.add_argument("--target", default=os.path.join(ROOT, "target_repo"))
    parser.add_argument("--output", default=os.path.join(HERE, "output"))
    args = parser.parse_args()

    if not os.path.isfile(args.spec):
        print(f"Error: spec file not found: {args.spec}")
        sys.exit(1)
    with open(args.spec) as f:
        spec = f.read().strip()

    if not os.path.isdir(args.target):
        print(f"Error: target repo not found: {args.target}")
        sys.exit(1)

    run(spec, args.target, args.output)


if __name__ == "__main__":
    main()
